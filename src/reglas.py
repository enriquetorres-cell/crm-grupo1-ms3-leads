"""Reglas de negocio de MS3 (Leads). Sin BD ni HTTP: se prueban solas."""
import datetime
import re
from decimal import Decimal

from .comun import DatoInvalido, ReglaNoCumplida, leer_iso

PUNTOS_FUENTE = {'referido': 30, 'web': 20, 'evento': 15, 'llamada_fria': 5}
PALABRAS_DECISOR = ('gerente', 'director', 'jefe', 'ceo', 'dueño', 'dueno', 'fundador', 'presidente')
UMBRAL_CALIENTE = 70
UMBRAL_TIBIO = 40
PUNTAJE_MINIMO_CONVERSION = 40
# Misma tabla que PLANES de MS1 (el plan viaja en el token). None = sin límite.
LIMITE_LEADS_MES = {'basico': 1000, 'pro': 10000, 'enterprise': None}

ESTADOS = ('nuevo', 'contactado', 'calificado', 'convertido', 'descartado')
# 'convertido' NO está aquí: solo se llega con POST /convertir, que publica el evento.
TRANSICIONES = {
    'nuevo': ('contactado', 'descartado'),
    'contactado': ('calificado', 'descartado'),
    'calificado': ('descartado',),
    'convertido': (),
    'descartado': (),
}
ABIERTOS = ('nuevo', 'contactado', 'calificado')

_PATRON_EMAIL = re.compile(r'[^@\s]+@[^@\s]+\.[a-z]{2,}', re.IGNORECASE)
_PATRON_TELEFONO = re.compile(r'\+?\d{7,12}')
_FACTORES_RUC = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)


def _ruc_valido(ruc):
    """Mismo algoritmo que MS2 (módulo 11 de SUNAT)."""
    if not re.fullmatch(r'(10|20)\d{9}', ruc):
        return False
    resto = 11 - sum(int(d) * f for d, f in zip(ruc, _FACTORES_RUC)) % 11
    return {10: 0, 11: 1}.get(resto, resto) == int(ruc[10])


def validar_lead(datos):
    """Normaliza un lead que llega por API o por CSV. Lanza DatoInvalido con todos los errores."""
    errores = []
    empresa = str(datos.get('empresa') or '').strip()
    if not empresa:
        errores.append('empresa es obligatoria')
    email = str(datos.get('email') or '').strip().lower() or None
    if email and not _PATRON_EMAIL.fullmatch(email):
        errores.append('email con formato inválido')
    telefono = re.sub(r'[\s-]', '', str(datos.get('telefono') or '')) or None
    if telefono and not _PATRON_TELEFONO.fullmatch(telefono):
        errores.append('teléfono con formato inválido')
    if not email and not telefono:
        errores.append('se necesita email o teléfono')
    fuente = str(datos.get('fuente') or 'web').strip().lower()
    if fuente not in PUNTOS_FUENTE:
        errores.append(f'fuente debe ser una de: {", ".join(PUNTOS_FUENTE)}')
    ruc = str(datos.get('ruc') or '').strip() or None
    if ruc and not _ruc_valido(ruc):
        errores.append('RUC inválido')
    try:
        empleados = int(datos.get('empleados') or 0)
        if empleados < 0:
            raise ValueError
    except (TypeError, ValueError):
        errores.append('empleados debe ser un entero >= 0')
        empleados = 0
    try:
        monto_estimado = Decimal(str(datos.get('monto_estimado') or 0))
        if monto_estimado < 0:
            raise ValueError
    except (ArithmeticError, ValueError):
        errores.append('monto_estimado debe ser un número >= 0')
        monto_estimado = Decimal('0')
    if errores:
        raise DatoInvalido('lead_invalido', errores)
    return {
        'empresa': empresa[:150],
        'nombre': str(datos.get('nombre') or '').strip()[:80] or None,
        'apellido': str(datos.get('apellido') or '').strip()[:80] or None,
        'cargo': str(datos.get('cargo') or '').strip()[:80] or None,
        'email': email,
        'telefono': telefono,
        'fuente': fuente,
        'ruc': ruc,
        'empleados': empleados,
        'monto_estimado': monto_estimado,
        'ultima_interaccion': datos.get('ultima_interaccion') or None,
    }


def _puntos_tamano(empleados):
    if empleados >= 200:
        return 25
    if empleados >= 50:
        return 15
    if empleados >= 10:
        return 8
    return 3


def puntaje(lead, ahora):
    """0 a 100: fuente + tamaño + cargo decisor + datos completos + interacción en los últimos 7 días."""
    total = PUNTOS_FUENTE[lead['fuente']] + _puntos_tamano(lead['empleados'])
    cargo = (lead.get('cargo') or '').lower()
    if any(palabra in cargo for palabra in PALABRAS_DECISOR):
        total += 20
    if lead.get('email') and lead.get('telefono'):
        total += 15
    if lead.get('ultima_interaccion'):
        if ahora - leer_iso(lead['ultima_interaccion'], 'ultima_interaccion') <= datetime.timedelta(days=7):
            total += 10
    return min(total, 100)


def temperatura(puntos):
    if puntos >= UMBRAL_CALIENTE:
        return 'caliente'
    if puntos >= UMBRAL_TIBIO:
        return 'tibio'
    return 'frio'


def elegir_vendedor(cargas):
    """Asignación: el vendedor activo con menos leads abiertos; empate -> el que entró primero.

    cargas = [{'vendedor': email, 'abiertos': n, 'orden': iso}, ...]
    """
    activos = [c for c in cargas if c.get('activo', True)]
    if not activos:
        return None
    return min(activos, key=lambda c: (int(c['abiertos']), c['orden']))['vendedor']


def validar_transicion(actual, nuevo, motivo=None):
    if nuevo not in ESTADOS:
        raise DatoInvalido('estado_invalido', f'Estados: {", ".join(ESTADOS)}')
    if nuevo == 'convertido':
        raise ReglaNoCumplida('usar_convertir', 'Para convertir usa POST /ms3/leads/{id}/convertir')
    if nuevo not in TRANSICIONES[actual]:
        raise ReglaNoCumplida('transicion_invalida', f'Un lead {actual} no puede pasar a {nuevo}')
    if nuevo == 'descartado' and not (motivo or '').strip():
        raise ReglaNoCumplida('falta_motivo', 'Indica por qué se descarta el lead')


def validar_conversion(lead):
    if lead['estado'] != 'calificado':
        raise ReglaNoCumplida('lead_no_calificado', 'Solo se convierte un lead calificado')
    if int(lead['puntaje']) < PUNTAJE_MINIMO_CONVERSION:
        raise ReglaNoCumplida('puntaje_insuficiente', f'Se necesita puntaje >= {PUNTAJE_MINIMO_CONVERSION}')


def validar_eliminacion(lead):
    """Un lead convertido ya generó su cuenta (MS2) y su oportunidad (MS4): no se borra."""
    if lead['estado'] == 'convertido':
        raise ReglaNoCumplida('lead_convertido', 'Un lead convertido no se elimina: ya tiene cuenta y oportunidad')


def limite_mensual(plan):
    return LIMITE_LEADS_MES.get(plan, LIMITE_LEADS_MES['basico'])
