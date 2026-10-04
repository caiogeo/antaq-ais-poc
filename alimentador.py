# -*- coding: utf-8 -*-
"""Alimentador AIS ao vivo, Brasil (POC de demonstração da Imagem Geosistemas para a ANTAQ).

A cada execução (GitHub Actions, a cada 5 min) escuta o aisstream.io por ESCUTA_S segundos (padrão 150) no retângulo do
Brasil e grava no ArcGIS Online, num serviço de feições privado:
  camada 0 (pontos)   última posição por MMSI, atualizada em vez de apagada; some quem não é visto há JANELA_MIN minutos;
  camada 1 (linhas)   rastro dos últimos JANELA_MIN minutos por MMSI (vértices e horários ficam no campo pontos_json do ponto);
  tabela 2 (situação) uma linha: hora de Brasília em texto e totais.
Chaves só por variável de ambiente (ARCGIS_API_KEY, AISSTREAM_KEY); nunca são impressas. Sem arcpy: requests, websockets, shapely.
"""
import asyncio
import datetime as dt
import json
import os
import sys
import time

import requests
import websockets
from shapely.geometry import Point, shape
from shapely.prepared import prep
from shapely.strtree import STRtree

AQUI = os.path.dirname(os.path.abspath(__file__))
SERVICO = os.environ.get("ARCGIS_SERVICO_URL",
                         "https://services.arcgis.com/4CZwpdWHGNPLU7QQ/arcgis/rest/services/ANTAQ_AIS_ao_vivo_POC/FeatureServer")
BBOX = (-74.5, -34.5, -28.5, 6.0)          # lon mín, lat mín, lon máx, lat máx (Brasil e mar adjacente)
ESCUTA_S = int(os.environ.get("ESCUTA_S", "150"))
JANELA_MIN = 15
FONTE = "aisstream.io (AIS em tempo real), Brasil"
RECORTE = "Brasil (lon %s a %s, lat %s a %s), GitHub Actions a cada 5 min, escuta de %d s, vistas nos últimos %d min" % (
    BBOX[0], BBOX[2], BBOX[1], BBOX[3], ESCUTA_S, JANELA_MIN)
# navStat do AIS (ITU-R M.1371), só os que importam para a leitura
NAVSTAT = {0: "navegando", 1: "fundeado", 2: "manobra limitada", 3: "manobra limitada", 5: "atracado", 7: "pescando", 8: "à vela", 15: "não informado"}
# tamanho dos campos texto (texto maior desfaz o lote inteiro no ArcGIS Online, erro 1003)
TAM = {"mmsi": 20, "nome": 80, "navstat": 30, "tipo": 10, "destino": 40, "fonte": 120, "no_brasil": 5, "porto_area": 120, "pontos_json": 8000}
TAM_RASTRO = {"mmsi": 20, "nome": 80}
LOTE = 500


def chave(nome):
    v = os.environ.get(nome)
    if not v and os.name == "nt":   # teste local no Windows: lê do registro do usuário (valor nunca é impresso)
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v = winreg.QueryValueEx(k, nome)[0]
        except OSError:
            v = None
    if not v:
        sys.exit(f"variável de ambiente {nome} não definida")
    return v


TOKEN = chave("ARCGIS_API_KEY")
AIS_KEY = chave("AISSTREAM_KEY")
S = requests.Session()


# ---------------------------------------------------------------- geografia (shapely)
def carregar_geo():
    g = json.load(open(os.path.join(AQUI, "dados", "brasil_ibge_minima.geojson"), encoding="utf-8"))
    brasil = shape(g["features"][0]["geometry"])
    fs = json.load(open(os.path.join(AQUI, "dados", "portos_br_area.geojson"), encoding="utf-8"))["features"]
    polis = [shape(f["geometry"]) for f in fs]
    nomes_p = [f["properties"].get("porto") for f in fs]
    return brasil, prep(brasil), STRtree(polis), polis, nomes_p


BRASIL, BRASIL_PREP, ARVORE, POLIS, NOMES_P = carregar_geo()


def situar(lon, lat):
    """("sim"|"não", km até o território brasileiro; 0 dentro). Graus para km: 111 km por grau (aproximação)."""
    p = Point(lon, lat)
    if BRASIL_PREP.contains(p):
        return "sim", 0.0
    return "não", round(BRASIL.distance(p) * 111.0, 1)


def em_porto(lon, lat):
    p = Point(lon, lat)
    for i in ARVORE.query(p):
        if POLIS[i].contains(p):
            return NOMES_P[i]
    return None


# ---------------------------------------------------------------- ArcGIS Online (REST, com a chave de API)
def rest(url, dados):
    d = dict(dados, f="json", token=TOKEN)
    r = S.post(url, data=d, timeout=90)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        sys.exit("erro do ArcGIS Online em %s: %s" % (url.replace(SERVICO, ""), str(j["error"])[:300]))
    return j


def consultar(camada, campos):
    """Todas as linhas (paginado), sem geometria."""
    url = f"{SERVICO}/{camada}/query"
    linhas, off = [], 0
    while True:
        j = rest(url, {"where": "1=1", "outFields": ",".join(campos), "returnGeometry": "false",
                       "resultOffset": off, "resultRecordCount": 2000})
        feats = j.get("features", [])
        linhas += [f["attributes"] for f in feats]
        if not j.get("exceededTransferLimit") or not feats:
            return linhas
        off += len(feats)


def limpar(attrs, tam):
    for c, v in list(attrs.items()):
        if isinstance(v, str):
            if "<" in v or ">" in v:   # "unsafe html content" no ArcGIS Online (erro 1006)
                v = v.replace("<", "‹").replace(">", "›")
            if c in tam and len(v) > tam[c]:
                v = v[:tam[c]]
            attrs[c] = v
    return attrs


def editar(camada, adds=(), updates=(), deletes=()):
    """applyEdits em lotes; devolve (ok_adds, ok_updates, ok_deletes) e imprime o primeiro erro de cada lote."""
    url = f"{SERVICO}/{camada}/applyEdits"
    adds, updates, deletes = list(adds), list(updates), list(deletes)
    tot = [0, 0, 0]
    n = max(len(adds), len(updates), len(deletes))
    for i in range(0, n, LOTE):
        p = {"rollbackOnFailure": "false"}
        if adds[i:i + LOTE]:
            p["adds"] = json.dumps(adds[i:i + LOTE], ensure_ascii=False)
        if updates[i:i + LOTE]:
            p["updates"] = json.dumps(updates[i:i + LOTE], ensure_ascii=False)
        if deletes[i:i + LOTE]:
            p["deletes"] = ",".join(str(x) for x in deletes[i:i + LOTE])
        if len(p) == 1:
            continue
        j = rest(url, p)
        for k, ch in enumerate(("addResults", "updateResults", "deleteResults")):
            rs = j.get(ch, [])
            tot[k] += sum(1 for x in rs if x.get("success"))
            erros = [x.get("error") for x in rs if not x.get("success")]
            if erros:
                print(f"   camada {camada}, {ch}: {len(erros)} com erro, ex.: {str(erros[0])[:200]}")
    return tuple(tot)


# ---------------------------------------------------------------- escuta do aisstream
ultimas, nomes_ws, hist = {}, {}, {}


async def escutar(segundos):
    fim = time.time() + segundos
    b = BBOX
    while time.time() < fim - 5:
        try:
            async with websockets.connect("wss://stream.aisstream.io/v0/stream", max_size=2 ** 22, open_timeout=20) as ws:
                await ws.send(json.dumps({"APIKey": AIS_KEY, "BoundingBoxes": [[[b[1], b[0]], [b[3], b[2]]]],
                                          "FilterMessageTypes": ["PositionReport", "StandardClassBPositionReport", "ShipStaticData"]}))
                while time.time() < fim:
                    try:
                        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(1, fim - time.time())))
                    except asyncio.TimeoutError:
                        break
                    meta = m.get("MetaData", {})
                    mmsi = str(meta.get("MMSI"))
                    if m.get("MessageType") == "ShipStaticData":
                        sd = m["Message"]["ShipStaticData"]
                        nomes_ws[mmsi] = ((sd.get("Name") or "").strip(), sd.get("Type"), (sd.get("Destination") or "").strip())
                        continue
                    msg = m["Message"].get("PositionReport") or m["Message"].get("StandardClassBPositionReport") or {}
                    lat, lon = meta.get("latitude"), meta.get("longitude")
                    if lat is None or lon is None:
                        continue
                    sog, cog = msg.get("Sog"), msg.get("Cog")
                    sog = None if sog is None or sog >= 102.2 else sog     # 102,3 nós = "não disponível" no AIS
                    cog = None if cog is None or cog >= 360 else cog       # 360 = "não disponível"
                    t = time.time()
                    ultimas[mmsi] = {"lon": lon, "lat": lat, "sog": sog, "cog": cog, "navstat": msg.get("NavigationalStatus", 15),
                                     "nome": (meta.get("ShipName") or "").strip(), "visto": t}
                    h = hist.setdefault(mmsi, [])
                    if not h or abs(h[-1][0] - lon) > 1e-5 or abs(h[-1][1] - lat) > 1e-5:
                        h.append((round(lon, 5), round(lat, 5), int(t)))
        except Exception as e:
            print("conexão com o aisstream caiu, tentando de novo em 5 s:", str(e)[:120])
            await asyncio.sleep(5)


# ---------------------------------------------------------------- gravação
def vertices(texto, novos, corte_s):
    """Vértices guardados no ponto (pontos_json) + os ouvidos agora, só os últimos JANELA_MIN min, no tamanho do campo."""
    try:
        pts = [tuple(p) for p in json.loads(texto or "[]")]
    except ValueError:
        pts = []
    for p in novos:
        if not pts or abs(pts[-1][0] - p[0]) > 1e-5 or abs(pts[-1][1] - p[1]) > 1e-5:
            pts.append(p)
    pts = [p for p in pts if p[2] >= corte_s]
    while pts and len(json.dumps(pts)) > TAM["pontos_json"]:
        pts.pop(0)
    return pts


def gravar_pontos(agora_ms, corte_s):
    """Última posição por MMSI (atualiza quem já existe, inclui quem é novo, remove quem não é visto há JANELA_MIN min).
    Devolve (atributos das embarcações na janela, {mmsi: vértices do rastro})."""
    existentes = consultar(0, ["objectid", "mmsi", "datahora", "navstat", "velocidade_nos", "no_brasil", "porto_area", "nome", "pontos_json"])
    por_mmsi, duplicados = {}, []
    for e in existentes:
        if e["mmsi"] in por_mmsi:
            duplicados.append(e["objectid"])
        else:
            por_mmsi[e["mmsi"]] = e
    adds, updates, mantidos, trilhas = [], [], [], {}
    for mmsi, u in ultimas.items():
        nome_, tipo, dest = nomes_ws.get(mmsi, (u["nome"], None, ""))
        no_br, dist = situar(u["lon"], u["lat"])
        ex = por_mmsi.get(mmsi)
        pts = vertices(ex.get("pontos_json") if ex else None, hist.get(mmsi, []), corte_s)
        trilhas[mmsi] = pts
        at = limpar({"mmsi": mmsi, "nome": nome_ or u["nome"] or (ex or {}).get("nome") or mmsi, "datahora": int(u["visto"] * 1000),
                     "velocidade_nos": u["sog"], "cog": u["cog"], "navstat": NAVSTAT.get(u["navstat"], "outro"),
                     "tipo": str(tipo) if tipo is not None else None, "destino": dest, "lon": u["lon"], "lat": u["lat"],
                     "fonte": FONTE, "atualizado_em": agora_ms, "no_brasil": no_br, "dist_brasil_km": dist,
                     "porto_area": em_porto(u["lon"], u["lat"]) if no_br == "sim" else None, "pontos_json": json.dumps(pts)}, TAM)
        f = {"geometry": {"x": u["lon"], "y": u["lat"], "spatialReference": {"wkid": 4326}}, "attributes": at}
        if ex:
            at["objectid"] = ex["objectid"]
            updates.append(f)
        else:
            adds.append(f)
        mantidos.append(at)
    deletes = list(duplicados)
    for mmsi, e in por_mmsi.items():
        if mmsi in ultimas:
            continue
        if (e.get("datahora") or 0) < corte_s * 1000:
            deletes.append(e["objectid"])
        else:   # vista numa execução anterior, ainda dentro da janela
            mantidos.append(e)
            trilhas[mmsi] = vertices(e.get("pontos_json"), [], corte_s)
    ok = editar(0, adds, updates, deletes)
    print(f"pontos: {len(adds)} novos, {len(updates)} atualizados, {len(deletes)} removidos (gravados: {ok}); na janela: {len(mantidos)}")
    return mantidos, trilhas


def gravar_rastro(agora_ms, mantidos, trilhas):
    """Linha por MMSI com 2 ou mais vértices distintos na janela; as demais saem."""
    existentes = {}
    for e in consultar(1, ["objectid", "mmsi"]):
        existentes.setdefault(e["mmsi"], []).append(e)
    nomes = {a["mmsi"]: a.get("nome") for a in mantidos}
    adds, updates, deletes = [], [], []
    for mmsi in set(trilhas) | set(existentes):
        regs = existentes.get(mmsi, [])
        deletes += [r["objectid"] for r in regs[1:]]
        pts = trilhas.get(mmsi, [])
        if len(pts) < 2:
            if regs:
                deletes.append(regs[0]["objectid"])
            continue
        at = limpar({"mmsi": mmsi, "nome": nomes.get(mmsi) or mmsi, "n_posicoes": len(pts),
                     "minutos": round((pts[-1][2] - pts[0][2]) / 60, 1), "atualizado_em": agora_ms}, TAM_RASTRO)
        f = {"geometry": {"paths": [[[p[0], p[1]] for p in pts]], "spatialReference": {"wkid": 4326}}, "attributes": at}
        if regs:
            at["objectid"] = regs[0]["objectid"]
            updates.append(f)
        else:
            adds.append(f)
    ok = editar(1, adds, updates, deletes)
    print(f"rastros: {len(adds)} novos, {len(updates)} atualizados, {len(deletes)} removidos (gravados: {ok})")


def gravar_status(agora, agora_ms, mantidos):
    nav = sum(1 for a in mantidos if a.get("navstat") == "navegando")
    vmax = max((a.get("velocidade_nos") or 0 for a in mantidos), default=0)
    n_br = sum(1 for a in mantidos if a.get("no_brasil") == "sim")
    n_porto = sum(1 for a in mantidos if a.get("porto_area"))
    local = agora.astimezone(dt.timezone(dt.timedelta(hours=-3)))
    at = {"atualizado_txt": local.strftime("%d/%m/%Y %H:%M") + " (Brasília)", "atualizado_em": agora_ms, "n_embarcacoes": len(mantidos),
          "n_navegando": nav, "vel_max_nos": round(vmax, 1), "fonte": FONTE, "recorte": RECORTE[:120], "n_no_brasil": n_br, "n_em_porto": n_porto}
    ex = consultar(2, ["objectid"])
    if ex:
        at["objectid"] = ex[0]["objectid"]
        editar(2, updates=[{"attributes": at}], deletes=[e["objectid"] for e in ex[1:]])
    else:
        editar(2, adds=[{"attributes": at}])
    print("situação: %s | embarcações %d | navegando %d | no Brasil %d | em porto %d | vel. máx. %s nós"
          % (at["atualizado_txt"], len(mantidos), nav, n_br, n_porto, round(vmax, 1)))


def main():
    print(f"escutando o aisstream por {ESCUTA_S} s (Brasil)...")
    asyncio.run(escutar(ESCUTA_S))
    print(f"embarcações ouvidas: {len(ultimas)} | com dados estáticos: {len(set(ultimas) & set(nomes_ws))}")
    agora = dt.datetime.now(dt.timezone.utc)
    agora_ms = int(agora.timestamp() * 1000)
    corte_s = int(agora.timestamp()) - JANELA_MIN * 60
    mantidos, trilhas = gravar_pontos(agora_ms, corte_s)
    gravar_rastro(agora_ms, mantidos, trilhas)
    gravar_status(agora, agora_ms, mantidos)


if __name__ == "__main__":
    main()
