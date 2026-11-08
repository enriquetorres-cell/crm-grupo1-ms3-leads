"""MS3 · Leads: captura, puntaje, asignación automática, estados e importación por CSV (S3).

DynamoDB, tabla de leads (partition key tenant_id):
  sk = LEAD#<id>        el lead
  sk = EMAIL#<email>    marca de unicidad (no hay dos leads con el mismo email en un tenant)
  sk = TEL#<telefono>   marca de unicidad del teléfono
  sk = MES#<aaaa-mm>    contador para el límite mensual del plan
Tabla de carga (tenant_id, vendedor): leads abiertos por vendedor, para la asignación.

Publica: lead.asignado, lead.convertido. Consume (cola crm-ms3): usuario.creado.
"""
import csv
import io
import os
import urllib.parse
import uuid

import boto3
from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from . import reglas
from .comun import (DatoInvalido, NoEncontrado, ReglaNoCumplida, a_json, ahora, consultar_pagina, enrutar,
                    iso, leer_cuerpo, log, parametro, procesar_cola, publicar, puede_ver, responder, ruta)

TABLA_LEADS = os.environ['TABLA_LEADS']
TABLA_CARGA = os.environ['TABLA_CARGA']
BUCKET = os.environ['BUCKET_IMPORTACIONES']

_dynamo = boto3.resource('dynamodb')
leads = _dynamo.Table(TABLA_LEADS)
carga = _dynamo.Table(TABLA_CARGA)
# Cliente de bajo nivel para las transacciones: recibe valores ya tipados con _tipado().
# (El _dynamo.meta.client del recurso los volvería a convertir y llegarían como mapas: ValidationException.)
cliente_dynamo = boto3.client('dynamodb')
s3 = boto3.client('s3')
_serializar = TypeSerializer().serialize


def _tipado(item):
    return {k: _serializar(v) for k, v in item.items()}


def _cargas(tenant_id):
    return carga.query(KeyConditionExpression=Key('tenant_id').eq(tenant_id))['Items']


def _cambio_de_carga(tenant_id, vendedor, delta):
    return {'Update': {
        'TableName': TABLA_CARGA,
        'Key': _tipado({'tenant_id': tenant_id, 'vendedor': vendedor}),
        'UpdateExpression': 'ADD abiertos :d',
        'ExpressionAttributeValues': _tipado({':d': delta}),
    }}


def _crear_lead(tenant_id, plan, datos, origen, cargas=None):
    """Valida, puntúa, asigna y guarda el lead en UNA transacción con sus marcas de unicidad,
    el contador mensual del plan y la carga del vendedor."""
    lead = reglas.validar_lead(datos)
    momento = ahora()
    puntos = reglas.puntaje(lead, momento)
    cargas = _cargas(tenant_id) if cargas is None else cargas
    vendedor = reglas.elegir_vendedor(cargas)
    lead_id = f'L{uuid.uuid4().hex[:12]}'
    item = {
        'tenant_id': tenant_id, 'sk': f'LEAD#{lead_id}', 'lead_id': lead_id, **lead,
        'puntaje': puntos, 'temperatura': reglas.temperatura(puntos), 'estado': 'nuevo',
        'propietario': vendedor, 'origen': origen, 'creado_en': iso(momento),
    }

    operaciones = [{'Put': {'TableName': TABLA_LEADS, 'Item': _tipado(item)}}]
    motivos = ['lead']
    for prefijo, valor in (('EMAIL', lead['email']), ('TEL', lead['telefono'])):
        if valor:
            operaciones.append({'Put': {
                'TableName': TABLA_LEADS,
                'Item': _tipado({'tenant_id': tenant_id, 'sk': f'{prefijo}#{valor}', 'lead_id': lead_id}),
                'ConditionExpression': 'attribute_not_exists(sk)',
            }})
            motivos.append('lead_duplicado')
    contador = {
        'TableName': TABLA_LEADS,
        'Key': _tipado({'tenant_id': tenant_id, 'sk': f'MES#{momento:%Y-%m}'}),
        'UpdateExpression': 'ADD #total :uno',
        'ExpressionAttributeNames': {'#total': 'total'},  # TOTAL es palabra reservada en DynamoDB
        'ExpressionAttributeValues': _tipado({':uno': 1}),
    }
    limite = reglas.limite_mensual(plan)
    if limite is not None:
        contador['ConditionExpression'] = 'attribute_not_exists(#total) OR #total < :limite'
        contador['ExpressionAttributeValues'] = _tipado({':uno': 1, ':limite': limite})
    operaciones.append({'Update': contador})
    motivos.append('limite_plan')
    if vendedor:
        operaciones.append(_cambio_de_carga(tenant_id, vendedor, 1))
        motivos.append('carga')

    try:
        cliente_dynamo.transact_write_items(TransactItems=operaciones)
    except ClientError as e:
        if e.response['Error']['Code'] != 'TransactionCanceledException':
            raise
        razones = e.response.get('CancellationReasons', [])
        fallo = next((motivos[i] for i, r in enumerate(razones) if r.get('Code') == 'ConditionalCheckFailed'), None)
        if fallo == 'lead_duplicado':
            raise ReglaNoCumplida('lead_duplicado', 'Ya hay un lead con ese email o teléfono')
        if fallo == 'limite_plan':
            raise ReglaNoCumplida('limite_plan', f'El plan {plan} permite {limite} leads al mes')
        raise

    if vendedor:
        for c in cargas:
            if c['vendedor'] == vendedor:
                c['abiertos'] = int(c['abiertos']) + 1
    publicar('lead.asignado', tenant_id, {
        'lead_id': lead_id, 'empresa': lead['empresa'], 'propietario': vendedor,
        'temperatura': item['temperatura'], 'puntaje': puntos, 'asignado_en': item['creado_en'],
    })
    return item


def _lead_visible(ctx, lead_id):
    lead = leads.get_item(Key={'tenant_id': ctx['tenant_id'], 'sk': f'LEAD#{lead_id}'}).get('Item')
    if not lead or not puede_ver(ctx, lead.get('propietario')):
        raise NoEncontrado('lead_no_encontrado', lead_id)
    return lead


def _actualizar_estado(ctx, lead, nuevo, extra=None):
    """Cambia el estado con bloqueo optimista y libera la carga del vendedor si el lead se cierra."""
    valores = {':nuevo': nuevo, ':actual': lead['estado'], ':ahora': iso(ahora())}
    asignaciones = ['estado = :nuevo', 'actualizado_en = :ahora']
    for campo, valor in (extra or {}).items():
        asignaciones.append(f'{campo} = :{campo}')
        valores[f':{campo}'] = valor
    operaciones = [{'Update': {
        'TableName': TABLA_LEADS,
        'Key': _tipado({'tenant_id': ctx['tenant_id'], 'sk': lead['sk']}),
        'UpdateExpression': 'SET ' + ', '.join(asignaciones),
        'ConditionExpression': 'estado = :actual',
        'ExpressionAttributeValues': _tipado(valores),
    }}]
    if lead.get('propietario') and nuevo not in reglas.ABIERTOS:
        operaciones.append(_cambio_de_carga(ctx['tenant_id'], lead['propietario'], -1))
    try:
        cliente_dynamo.transact_write_items(TransactItems=operaciones)
    except ClientError as e:
        if e.response['Error']['Code'] == 'TransactionCanceledException':
            raise ReglaNoCumplida('estado_cambiado', 'Otro usuario cambió este lead; vuelve a cargarlo')
        raise
    return {**lead, 'estado': nuevo, **(extra or {})}


# ---------- Rutas ----------

def salud(event, ctx):
    return responder(200, {'status': 'ok', 'servicio': 'ms3'})


def listar_leads(event, ctx):
    filtro = None
    for campo in ('estado', 'temperatura'):
        if parametro(event, campo):
            condicion = Attr(campo).eq(parametro(event, campo))
            filtro = condicion if filtro is None else filtro & condicion
    if ctx['rol'] == 'vendedor':
        condicion = Attr('propietario').eq(ctx['usuario_id'])
        filtro = condicion if filtro is None else filtro & condicion
    kwargs = {'KeyConditionExpression': Key('tenant_id').eq(ctx['tenant_id']) & Key('sk').begins_with('LEAD#')}
    if filtro is not None:
        kwargs['FilterExpression'] = filtro
    return responder(200, consultar_pagina(leads, event, **kwargs))


def obtener_lead(event, ctx):
    return responder(200, _lead_visible(ctx, ruta(event, 'id')))


def crear_lead(event, ctx):
    return responder(201, _crear_lead(ctx['tenant_id'], ctx['plan'], leer_cuerpo(event), origen='api'))


def cambiar_estado(event, ctx):
    datos = leer_cuerpo(event)
    lead = _lead_visible(ctx, ruta(event, 'id'))
    reglas.validar_transicion(lead['estado'], datos.get('estado'), datos.get('motivo'))
    extra = {'motivo_descarte': datos['motivo'].strip()} if datos.get('estado') == 'descartado' else None
    return responder(200, _actualizar_estado(ctx, lead, datos['estado'], extra))


def convertir(event, ctx):
    lead = _lead_visible(ctx, ruta(event, 'id'))
    reglas.validar_conversion(lead)
    lead = _actualizar_estado(ctx, lead, 'convertido', {'convertido_en': iso(ahora())})
    publicar('lead.convertido', ctx['tenant_id'], {k: lead.get(k) for k in (
        'lead_id', 'empresa', 'ruc', 'nombre', 'apellido', 'cargo', 'email', 'telefono',
        'propietario', 'monto_estimado')})
    return responder(200, lead)


def pedir_importacion(event, ctx):
    """Devuelve una URL firmada para subir el CSV directo a S3 (vence en 15 min)."""
    importacion_id = uuid.uuid4().hex
    key = f'entrada/{ctx["tenant_id"]}/{ctx["plan"]}/{importacion_id}.csv'
    url = s3.generate_presigned_url(
        'put_object', Params={'Bucket': BUCKET, 'Key': key, 'ContentType': 'text/csv'}, ExpiresIn=900)
    return responder(201, {
        'importacion_id': importacion_id, 'url': url, 'metodo': 'PUT',
        'cabeceras': {'Content-Type': 'text/csv'},
        'columnas': ['empresa', 'nombre', 'apellido', 'cargo', 'email', 'telefono', 'fuente', 'ruc',
                     'empleados', 'monto_estimado'],
    })


def ver_importacion(event, ctx):
    key = f'resultados/{ctx["tenant_id"]}/{ruta(event, "id")}.json'
    try:
        cuerpo = s3.get_object(Bucket=BUCKET, Key=key)['Body'].read()
    except ClientError as e:
        if e.response['Error']['Code'] in ('NoSuchKey', '404'):
            return responder(200, {'importacion_id': ruta(event, 'id'), 'estado': 'procesando'})
        raise
    return {'statusCode': 200, 'headers': {'Content-Type': 'application/json; charset=utf-8'}, 'body': cuerpo.decode()}


RUTAS = {
    'GET /ms3/health': salud,
    'GET /ms3/leads': listar_leads,
    'GET /ms3/leads/{id}': obtener_lead,
    'POST /ms3/leads': crear_lead,
    'PATCH /ms3/leads/{id}/estado': cambiar_estado,
    'POST /ms3/leads/{id}/convertir': convertir,
    'POST /ms3/importaciones': pedir_importacion,
    'GET /ms3/importaciones/{id}': ver_importacion,
}


def main(event, _context):
    return enrutar(RUTAS, event)


# ---------- Importación (evento de S3: entrada/<tenant>/<plan>/<id>.csv) ----------

def importar(event, _context):
    for registro in event['Records']:
        key = urllib.parse.unquote_plus(registro['s3']['object']['key'])
        partes = key.split('/')
        if len(partes) != 4:
            log.warning('Clave inesperada %s', key)
            continue
        _, tenant_id, plan, archivo = partes
        importacion_id = archivo.removesuffix('.csv')
        texto = s3.get_object(Bucket=BUCKET, Key=key)['Body'].read().decode('utf-8-sig')

        cargas = _cargas(tenant_id)
        creados, rechazados, limite_alcanzado = 0, [], False
        for numero, fila in enumerate(csv.DictReader(io.StringIO(texto)), start=2):
            if limite_alcanzado:
                rechazados.append({'fila': numero, 'motivo': 'limite_plan'})
                continue
            try:
                _crear_lead(tenant_id, plan, fila, origen='csv', cargas=cargas)
                creados += 1
            except DatoInvalido as e:
                detalle = '; '.join(e.detalle) if isinstance(e.detalle, list) else e.error
                rechazados.append({'fila': numero, 'motivo': detalle})
                limite_alcanzado = e.error == 'limite_plan'

        errores_key = None
        if rechazados:
            salida = io.StringIO()
            escritor = csv.DictWriter(salida, fieldnames=['fila', 'motivo'])
            escritor.writeheader()
            escritor.writerows(rechazados)
            errores_key = f'errores/{tenant_id}/{importacion_id}.csv'
            s3.put_object(Bucket=BUCKET, Key=errores_key, Body=salida.getvalue().encode('utf-8'), ContentType='text/csv')
        resumen = {'importacion_id': importacion_id, 'estado': 'terminada', 'creados': creados,
                   'rechazados': len(rechazados), 'errores_key': errores_key}
        s3.put_object(Bucket=BUCKET, Key=f'resultados/{tenant_id}/{importacion_id}.json',
                      Body=a_json(resumen).encode('utf-8'), ContentType='application/json')
        log.info('Importación %s: %s', importacion_id, resumen)


# ---------- Consumidor: usuario.creado -> el vendedor entra al reparto de leads ----------

def _manejar(mensaje):
    datos = mensaje['datos']
    if mensaje['evento'] != 'usuario.creado' or datos.get('rol') != 'vendedor':
        return
    try:
        carga.put_item(
            Item={'tenant_id': mensaje['tenant_id'], 'vendedor': datos['email'], 'abiertos': 0,
                  'orden': mensaje['ocurrido_en'], 'activo': True},
            ConditionExpression='attribute_not_exists(vendedor)',
        )
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise  # si ya existía, el mensaje llegó repetido: no se hace nada


def consumidor(event, _context):
    return procesar_cola(event, _manejar)
