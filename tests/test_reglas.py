import datetime

import pytest

from src import reglas
from src.comun import DatoInvalido, ReglaNoCumplida

AHORA = datetime.datetime(2026, 11, 5, 15, 0, tzinfo=datetime.timezone.utc)


def lead(**cambios):
    base = {'empresa': 'Andina Seguros', 'email': 'ana@andina.pe', 'telefono': '987654321',
            'fuente': 'referido', 'empleados': 250, 'cargo': 'Gerente comercial'}
    return reglas.validar_lead({**base, **cambios})


def test_lead_ideal_llega_a_90_y_es_caliente():
    # referido 30 + 250 empleados 25 + gerente 20 + email y teléfono 15 = 90
    puntos = reglas.puntaje(lead(), AHORA)
    assert puntos == 90
    assert reglas.temperatura(puntos) == 'caliente'


def test_interaccion_reciente_suma_y_el_puntaje_no_pasa_de_100():
    assert reglas.puntaje(lead(ultima_interaccion='2026-11-03T10:00:00Z'), AHORA) == 100


def test_lead_frio():
    puntos = reglas.puntaje(lead(fuente='llamada_fria', empleados=3, cargo='Asistente', telefono=None), AHORA)
    assert puntos == 8
    assert reglas.temperatura(puntos) == 'frio'


def test_validar_lead_junta_todos_los_errores():
    with pytest.raises(DatoInvalido) as error:
        reglas.validar_lead({'fuente': 'tiktok', 'ruc': '20131312954'})
    assert len(error.value.detalle) == 4


def test_asignacion_al_vendedor_con_menos_carga():
    cargas = [
        {'vendedor': 'a@x.pe', 'abiertos': 5, 'orden': '2026-10-01T00:00:00Z'},
        {'vendedor': 'b@x.pe', 'abiertos': 2, 'orden': '2026-10-03T00:00:00Z'},
        {'vendedor': 'c@x.pe', 'abiertos': 2, 'orden': '2026-10-02T00:00:00Z'},
        {'vendedor': 'd@x.pe', 'abiertos': 0, 'orden': '2026-10-01T00:00:00Z', 'activo': False},
    ]
    assert reglas.elegir_vendedor(cargas) == 'c@x.pe'
    assert reglas.elegir_vendedor([]) is None


def test_transiciones_de_estado():
    reglas.validar_transicion('nuevo', 'contactado')
    with pytest.raises(ReglaNoCumplida):
        reglas.validar_transicion('nuevo', 'calificado')
    with pytest.raises(ReglaNoCumplida):
        reglas.validar_transicion('calificado', 'convertido')
    with pytest.raises(ReglaNoCumplida):
        reglas.validar_transicion('contactado', 'descartado')
    reglas.validar_transicion('contactado', 'descartado', motivo='Sin presupuesto')


def test_solo_se_convierte_un_calificado_con_puntaje_suficiente():
    reglas.validar_conversion({'estado': 'calificado', 'puntaje': 40})
    with pytest.raises(ReglaNoCumplida):
        reglas.validar_conversion({'estado': 'calificado', 'puntaje': 39})
    with pytest.raises(ReglaNoCumplida):
        reglas.validar_conversion({'estado': 'contactado', 'puntaje': 90})


def test_un_lead_convertido_no_se_elimina():
    with pytest.raises(ReglaNoCumplida):
        reglas.validar_eliminacion({'estado': 'convertido'})
    for estado in ('nuevo', 'contactado', 'calificado', 'descartado'):
        reglas.validar_eliminacion({'estado': estado})


def test_limite_mensual_por_plan():
    assert reglas.limite_mensual('basico') == 1000
    assert reglas.limite_mensual('enterprise') is None
