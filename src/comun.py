"""Utilidades comunes del CRM: respuestas, errores, tenant del token y eventos.

Cada microservicio se empaqueta por separado, así que este archivo se COPIA
igual en cada `msN-*/src/comun.py` con `sincronizar-comun.ps1`.
Edítalo solo aquí (`comun/comun.py`) y vuelve a sincronizar.

No importa boto3 al cargar: `reglas.py` lo usa y sus tests corren sin AWS.
"""
import base64
import binascii
import datetime
import decimal
import json
import logging
import os
import uuid

log = logging.getLogger()
log.setLevel(logging.INFO)


# ---------- Errores de negocio (las reglas los lanzan; enrutar() los traduce a HTTP) ----------

class DatoInvalido(Exception):
    """El dato no tiene el formato esperado -> 422."""

    def __init__(self, error, detalle=None):
        super().__init__(error)
        self.error = error
        self.detalle = detalle


class ReglaNoCumplida(DatoInvalido):
    """El dato es válido, pero una regla de negocio lo impide -> 409."""


class NoEncontrado(DatoInvalido):
    """No existe en ESTE tenant -> 404 (nunca 403, para no revelar que existe en otro)."""


class SinPermiso(DatoInvalido):
    """El rol del token no alcanza -> 403."""


_STATUS = ((ReglaNoCumplida, 409), (NoEncontrado, 404), (SinPermiso, 403), (DatoInvalido, 422))


# ---------- Fechas y JSON ----------

def ahora():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(fecha):
    return fecha.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def leer_iso(texto, campo='fecha'):
    try:
        fecha = datetime.datetime.fromisoformat(str(texto).replace('Z', '+00:00'))
    except ValueError:
        raise DatoInvalido('fecha_invalida', f'{campo} debe ser ISO 8601, p. ej. 2026-11-05T15:00:00Z')
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=datetime.timezone.utc)
    return fecha


def _json_default(valor):
    if isinstance(valor, decimal.Decimal):
        return int(valor) if valor == valor.to_integral_value() else float(valor)
    if isinstance(valor, datetime.datetime):
        return iso(valor)
    if isinstance(valor, datetime.date):
        return valor.isoformat()
    if isinstance(valor, (set, frozenset)):
        return sorted(valor)
    if isinstance(valor, uuid.UUID):
        return str(valor)
    raise TypeError(f'No se puede serializar {type(valor).__name__}')


def a_json(valor):
    return json.dumps(valor, default=_json_default, ensure_ascii=False)


# ---------- HTTP (payload 2.0 de API Gateway HTTP API) ----------

def responder(status, cuerpo):
    return {
        'statusCode': status,
        'headers': {'Content-Type': 'application/json; charset=utf-8'},
        'body': a_json(cuerpo),
    }


def responder_error(status, error, detalle=None):
    return responder(status, {'error': error, 'detalle': detalle})


def leer_cuerpo(event):
    crudo = event.get('body') or '{}'
    if event.get('isBase64Encoded'):
        crudo = base64.b64decode(crudo).decode('utf-8')
    try:
        datos = json.loads(crudo, parse_float=decimal.Decimal)
    except json.JSONDecodeError:
        raise DatoInvalido('json_invalido', 'El cuerpo no es JSON válido')
    if not isinstance(datos, dict):
        raise DatoInvalido('json_invalido', 'El cuerpo debe ser un objeto JSON')
    return datos


def parametro(event, nombre, por_defecto=None):
    return (event.get('queryStringParameters') or {}).get(nombre, por_defecto)


def ruta(event, nombre):
    return (event.get('pathParameters') or {}).get(nombre)


def entero(valor, campo, minimo=None, maximo=None):
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        raise DatoInvalido('numero_invalido', f'{campo} debe ser un entero')
    if (minimo is not None and numero < minimo) or (maximo is not None and numero > maximo):
        raise DatoInvalido('numero_fuera_de_rango', f'{campo} debe estar entre {minimo} y {maximo}')
    return numero


def paginacion(event):
    """?page=&limit= (Contrato Cero). limit máximo 100."""
    page = entero(parametro(event, 'page', 1), 'page', minimo=1)
    limit = entero(parametro(event, 'limit', 20), 'limit', minimo=1, maximo=100)
    return page, limit


def contexto(event):
    """Lo que dejó el Authorizer de MS1: tenant_id, usuario_id (email), rol y plan.

    El tenant SIEMPRE sale de aquí, nunca del body ni de la URL.
    """
    return ((event.get('requestContext') or {}).get('authorizer') or {}).get('lambda') or {}


def exigir_rol(ctx, *roles):
    if ctx.get('rol') not in roles:
        raise SinPermiso('rol_insuficiente', f'Se requiere rol {" o ".join(roles)}')


def puede_ver(ctx, propietario):
    """Un vendedor solo ve lo suyo; gerente y admin ven todo el tenant."""
    return ctx.get('rol') in ('gerente', 'admin') or propietario == ctx.get('usuario_id')


def enrutar(rutas, event):
    """rutas = {'GET /ms2/cuentas': funcion(event, ctx), ...}"""
    funcion = rutas.get(event.get('routeKey'))
    if funcion is None:
        return responder_error(404, 'ruta_no_encontrada', event.get('routeKey'))
    try:
        return funcion(event, contexto(event))
    except DatoInvalido as e:
        status = next(s for clase, s in _STATUS if isinstance(e, clase))
        return responder_error(status, e.error, e.detalle)
    except Exception:
        log.exception('Error no controlado en %s', event.get('routeKey'))
        return responder_error(500, 'error_interno', 'Revisa los logs de CloudWatch')


# ---------- DynamoDB: listados con cursor ----------

def consultar_pagina(tabla, event, **kwargs):
    """Listado de DynamoDB: {limit, items, siguiente}.

    DynamoDB no salta a una página N; en vez de ?page= usa ?siguiente=<cursor>
    que devuelve la respuesta anterior.
    """
    limit = entero(parametro(event, 'limit', 20), 'limit', minimo=1, maximo=100)
    cursor = parametro(event, 'siguiente')
    if cursor:
        try:
            kwargs['ExclusiveStartKey'] = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        except (ValueError, binascii.Error):
            raise DatoInvalido('cursor_invalido', 'siguiente no es un cursor válido')
    respuesta = tabla.query(Limit=limit, **kwargs)
    ultima = respuesta.get('LastEvaluatedKey')
    return {
        'limit': limit,
        'items': respuesta['Items'],
        'siguiente': base64.urlsafe_b64encode(a_json(ultima).encode()).decode() if ultima else None,
    }


# ---------- Eventos (SNS -> SQS) ----------

_sns = None


def _cliente_sns():
    global _sns
    if _sns is None:
        import boto3
        _sns = boto3.client('sns')
    return _sns


def publicar(evento, tenant_id, datos):
    """Publica en crm-eventos con el formato acordado. El atributo `evento` filtra las colas."""
    mensaje = {
        'evento': evento,
        'tenant_id': tenant_id,
        'id': str(uuid.uuid4()),
        'ocurrido_en': iso(ahora()),
        'datos': datos,
    }
    _cliente_sns().publish(
        TopicArn=os.environ['TOPICO_EVENTOS'],
        Message=a_json(mensaje),
        MessageAttributes={'evento': {'DataType': 'String', 'StringValue': evento}},
    )
    log.info('Evento publicado %s %s', evento, mensaje['id'])
    return mensaje


def notificar(asunto, texto, tenant_id):
    """Aviso por email (tópico crm-notificaciones)."""
    _cliente_sns().publish(
        TopicArn=os.environ['TOPICO_NOTIFICACIONES'],
        Subject=asunto[:100],
        Message=texto,
        MessageAttributes={'tenant_id': {'DataType': 'String', 'StringValue': tenant_id}},
    )


def procesar_cola(event, manejar):
    """Consumidor SQS con ReportBatchItemFailures: solo se reintentan los mensajes que fallaron.

    `manejar(mensaje)` debe ser idempotente: SQS puede entregar el mismo mensaje dos veces.
    """
    fallidos = []
    for registro in event.get('Records', []):
        try:
            manejar(json.loads(registro['body'], parse_float=decimal.Decimal))
        except Exception:
            log.exception('Falló el mensaje %s', registro.get('messageId'))
            fallidos.append({'itemIdentifier': registro['messageId']})
    return {'batchItemFailures': fallidos}
