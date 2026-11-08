"""consultar_pagina con filtro: debe llenar la página aunque DynamoDB lea muchos ítems que no pasan."""
import base64
import json

from src.comun import consultar_pagina


class TablaFalsa:
    """Imita Table.query: Limit cuenta los ítems LEÍDOS; el filtro se aplica después."""

    def __init__(self, items, pasa):
        self.items, self.pasa, self.queries = items, pasa, 0

    def query(self, Limit, ExclusiveStartKey=None, FilterExpression=None, **_):
        self.queries += 1
        inicio = 0
        if ExclusiveStartKey:
            inicio = next(i for i, it in enumerate(self.items) if it['sk'] == ExclusiveStartKey['sk']) + 1
        leidos = self.items[inicio:inicio + Limit]
        respuesta = {'Items': [it for it in leidos if FilterExpression is None or self.pasa(it)]}
        if inicio + Limit < len(self.items):
            respuesta['LastEvaluatedKey'] = {'tenant_id': 't', 'sk': leidos[-1]['sk']}
        return respuesta


def evento(**query):
    return {'queryStringParameters': {k: str(v) for k, v in query.items()}}


def cursor(pagina):
    return json.loads(base64.urlsafe_b64decode(pagina['siguiente'])) if pagina['siguiente'] else None


ITEMS = [{'tenant_id': 't', 'sk': f'LEAD#{i:05d}', 'mio': i % 50 == 0} for i in range(1000)]  # 20 "míos"


def test_con_filtro_llena_la_pagina_aunque_los_primeros_no_pasen():
    tabla = TablaFalsa(ITEMS, lambda it: it['mio'])
    pagina = consultar_pagina(tabla, evento(limit=5), claves=('tenant_id', 'sk'), FilterExpression='mio')
    assert [it['sk'] for it in pagina['items']] == ['LEAD#00000', 'LEAD#00050', 'LEAD#00100', 'LEAD#00150', 'LEAD#00200']
    assert cursor(pagina) == {'tenant_id': 't', 'sk': 'LEAD#00200'}  # corta justo en el último devuelto


def test_el_cursor_continua_sin_saltar_ni_repetir():
    tabla = TablaFalsa(ITEMS, lambda it: it['mio'])
    vistos, siguiente = [], None
    for _ in range(10):
        ev = evento(limit=7, **({'siguiente': siguiente} if siguiente else {}))
        pagina = consultar_pagina(tabla, ev, claves=('tenant_id', 'sk'), FilterExpression='mio')
        vistos += [it['sk'] for it in pagina['items']]
        siguiente = pagina['siguiente']
        if not siguiente:
            break
    assert vistos == [f'LEAD#{i:05d}' for i in range(0, 1000, 50)]  # los 20, en orden, sin duplicados


def test_sin_filtro_es_una_sola_query():
    tabla = TablaFalsa(ITEMS, lambda it: True)
    pagina = consultar_pagina(tabla, evento(limit=20))
    assert len(pagina['items']) == 20 and tabla.queries == 1


def test_filtro_muy_selectivo_respeta_el_tope_de_lecturas():
    tabla = TablaFalsa(ITEMS * 3, lambda it: False)  # nada pasa el filtro
    pagina = consultar_pagina(tabla, evento(limit=20), claves=('tenant_id', 'sk'), FilterExpression='x')
    assert pagina['items'] == [] and tabla.queries == 10 and pagina['siguiente']  # se puede seguir con el cursor
