"""Estratégia de análise de candles para entradas na Quotex (timeframe M5).

Combina tendência (médias móveis 9/21, confirmada no M15), regiões de
suporte/resistência, padrões de candle (Martelo, Estrela Cadente, Engolfo
de alta/baixa, Doji) e RSI(14) num placar de confiança de 0 a 100. Uma
entrada só é sugerida quando a confiança mínima configurada é atingida.

Este módulo não depende de nenhuma biblioteca externa além do que a Quotex
já devolve nas velas (open/close/high/low) — sem numpy/pandas, para manter
o bot leve e fácil de rodar em qualquer host.
"""
import logging

logger = logging.getLogger(__name__)

CONFIANCA_MINIMA_PADRAO = 75.0
MEDIA_RAPIDA = 9
MEDIA_LENTA = 21
RSI_PERIODOS = 14


def _sma(valores, periodo):
    """Média móvel simples dos últimos `periodo` valores."""
    if len(valores) < periodo:
        return None
    janela = valores[-periodo:]
    return sum(janela) / periodo


def _rsi(fechamentos, periodos=RSI_PERIODOS):
    """RSI clássico (Wilder) sobre a lista de preços de fechamento."""
    if len(fechamentos) < periodos + 1:
        return None

    ganhos, perdas = [], []
    for i in range(1, len(fechamentos)):
        delta = fechamentos[i] - fechamentos[i - 1]
        ganhos.append(max(delta, 0))
        perdas.append(max(-delta, 0))

    media_ganho = sum(ganhos[:periodos]) / periodos
    media_perda = sum(perdas[:periodos]) / periodos

    for i in range(periodos, len(ganhos)):
        media_ganho = (media_ganho * (periodos - 1) + ganhos[i]) / periodos
        media_perda = (media_perda * (periodos - 1) + perdas[i]) / periodos

    if media_perda == 0:
        return 100.0
    rs = media_ganho / media_perda
    return 100 - (100 / (1 + rs))


def _normalizar_velas(candles):
    """Converte as velas cruas da Quotex em dicts numéricos, ignorando as inválidas."""
    normalizadas = []
    for c in candles:
        try:
            normalizadas.append({
                "open": float(c["open"]),
                "close": float(c["close"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return normalizadas


def _tendencia(fechamentos):
    """Retorna 'alta', 'baixa' ou None a partir do cruzamento das médias 9/21
    e de pelo menos 3 fechamentos consecutivos no mesmo lado da média lenta."""
    rapida = _sma(fechamentos, MEDIA_RAPIDA)
    lenta = _sma(fechamentos, MEDIA_LENTA)
    if rapida is None or lenta is None or len(fechamentos) < MEDIA_LENTA + 3:
        return None

    ultimos_3 = fechamentos[-3:]

    if rapida > lenta and all(f > lenta for f in ultimos_3):
        return "alta"
    if rapida < lenta and all(f < lenta for f in ultimos_3):
        return "baixa"
    return None


def _suporte_resistencia(velas, n=5):
    """Últimos `n` topos (máximas locais) e fundos (mínimas locais)."""
    topos, fundos = [], []
    for i in range(1, len(velas) - 1):
        anterior, atual, proximo = velas[i - 1], velas[i], velas[i + 1]
        if atual["high"] > anterior["high"] and atual["high"] > proximo["high"]:
            topos.append(atual["high"])
        if atual["low"] < anterior["low"] and atual["low"] < proximo["low"]:
            fundos.append(atual["low"])
    return topos[-n:], fundos[-n:]


def _perto_de(preco, niveis, tolerancia_pct=0.05):
    """Verifica se `preco` está a até `tolerancia_pct`% de algum nível."""
    for nivel in niveis:
        if nivel == 0:
            continue
        distancia_pct = abs(preco - nivel) / nivel * 100
        if distancia_pct <= tolerancia_pct:
            return True
    return False


def _corpo(vela):
    return abs(vela["close"] - vela["open"])


def _sombra_superior(vela):
    return vela["high"] - max(vela["open"], vela["close"])


def _sombra_inferior(vela):
    return min(vela["open"], vela["close"]) - vela["low"]


def _padrao_candle(velas):
    """Detecta o padrão de candle mais recente. Retorna (nome, sinal) ou (None, None).

    sinal é 'call', 'put' ou None (Doji não gera sinal sozinho).
    """
    if len(velas) < 2:
        return None, None

    atual = velas[-1]
    anterior = velas[-2]
    corpo_atual = _corpo(atual)
    faixa_atual = atual["high"] - atual["low"]

    if faixa_atual <= 0:
        return None, None

    # Doji: corpo muito pequeno perto do tamanho total da vela.
    if corpo_atual / faixa_atual < 0.1:
        return "doji", None

    # Martelo: pavio inferior >= 2x o corpo, corpo pequeno, fecha perto da máxima.
    if (
        _sombra_inferior(atual) >= 2 * corpo_atual
        and corpo_atual / faixa_atual < 0.35
        and (atual["high"] - atual["close"]) / faixa_atual < 0.15
    ):
        return "martelo", "call"

    # Estrela Cadente: pavio superior >= 2x o corpo, corpo pequeno, fecha perto da mínima.
    if (
        _sombra_superior(atual) >= 2 * corpo_atual
        and corpo_atual / faixa_atual < 0.35
        and (atual["close"] - atual["low"]) / faixa_atual < 0.15
    ):
        return "estrela_cadente", "put"

    # Engolfo de alta: vermelho seguido de verde que cobre totalmente o corpo anterior.
    anterior_vermelho = anterior["close"] < anterior["open"]
    atual_verde = atual["close"] > atual["open"]
    if (
        anterior_vermelho and atual_verde
        and atual["close"] >= anterior["open"] and atual["open"] <= anterior["close"]
    ):
        return "engolfo_alta", "call"

    # Engolfo de baixa: verde seguido de vermelho que cobre totalmente o corpo anterior.
    anterior_verde = anterior["close"] > anterior["open"]
    atual_vermelho = atual["close"] < atual["open"]
    if (
        anterior_verde and atual_vermelho
        and atual["open"] >= anterior["close"] and atual["close"] <= anterior["open"]
    ):
        return "engolfo_baixa", "put"

    return None, None


def _candle_muito_pequena(velas, limiar_pct=0.02):
    """True se a última vela tem corpo desprezível (mercado lateral/indeciso)."""
    atual = velas[-1]
    if atual["close"] == 0:
        return True
    return _corpo(atual) / atual["close"] * 100 < limiar_pct


def _mercado_lateralizado(fechamentos, janela=10, limiar_pct=0.15):
    """True se a variação percentual dos últimos `janela` fechamentos for pequena."""
    if len(fechamentos) < janela:
        return False
    recorte = fechamentos[-janela:]
    variacao_pct = (max(recorte) - min(recorte)) / min(recorte) * 100 if min(recorte) else 0
    return variacao_pct < limiar_pct


def analisar(velas_m5, velas_m15, confianca_minima=CONFIANCA_MINIMA_PADRAO):
    """
    Modelo adaptativo por pontuação.

    Mantém compatibilidade total:
    retorno:
    {
        "direcao": "call" | "put" | None,
        "confianca": float,
        "motivo_ignorado": str | None,
        "detalhes": {}
    }
    """

    velas_m5 = _normalizar_velas(velas_m5)
    velas_m15 = _normalizar_velas(velas_m15)

    detalhes = {}

    if len(velas_m5) < MEDIA_LENTA + 3 or len(velas_m15) < MEDIA_LENTA + 3:

        logger.info(
            "Ignorado: dados_insuficientes | %s",
            detalhes
        )

        return {
            "direcao": None,
            "confianca": 0.0,
            "motivo_ignorado": "dados_insuficientes",
            "detalhes": detalhes
        }


    fechamentos_m5 = [
        v["close"] for v in velas_m5
    ]

    fechamentos_m15 = [
        v["close"] for v in velas_m15
    ]


    pontos_call = 0
    pontos_put = 0


    # ==========================
    # 1 - Tendência M15 +30
    # ==========================

    tendencia_m15 = _tendencia(
        fechamentos_m15
    )

    tendencia_m5 = _tendencia(
        fechamentos_m5
    )


    detalhes["tendencia_m15"] = tendencia_m15
    detalhes["tendencia_m5"] = tendencia_m5


    if tendencia_m15 == "alta":
        pontos_call += 30

    elif tendencia_m15 == "baixa":
        pontos_put += 30



    # ==========================
    # 2 - Confirmação M5 +20
    # ==========================

    if tendencia_m5 == "alta":
        pontos_call += 20

    elif tendencia_m5 == "baixa":
        pontos_put += 20



    # ==========================
    # 3 - RSI +20
    # ==========================

    rsi = _rsi(
        fechamentos_m5
    )


    detalhes["rsi"] = (
        round(rsi,2)
        if rsi is not None
        else None
    )


    if rsi is not None:

        # sobrevenda favorece CALL
        if rsi < 40:
            pontos_call += 20


        # sobrecompra favorece PUT
        elif rsi > 60:
            pontos_put += 20



    # ==========================
    # 4 - Padrão Candle +20
    # ==========================

    padrao, sinal_padrao = _padrao_candle(
        velas_m5
    )


    detalhes["padrao"] = padrao


    if sinal_padrao == "call":

        pontos_call += 20


    elif sinal_padrao == "put":

        pontos_put += 20



    # ==========================
    # 5 - Suporte / Resistência +10
    # ==========================

    topos, fundos = _suporte_resistencia(
        velas_m5
    )


    preco = velas_m5[-1]["close"]


    perto_suporte = _perto_de(
        preco,
        fundos
    )


    perto_resistencia = _perto_de(
        preco,
        topos
    )


    detalhes["perto_suporte"] = perto_suporte
    detalhes["perto_resistencia"] = perto_resistencia


    if perto_suporte:
        pontos_call += 10


    if perto_resistencia:
        pontos_put += 10



    # ==========================
    # Resultado final
    # ==========================

    detalhes["pontos_call"] = pontos_call
    detalhes["pontos_put"] = pontos_put


    if pontos_call > pontos_put:

        direcao = "call"
        confianca = pontos_call


    elif pontos_put > pontos_call:

        direcao = "put"
        confianca = pontos_put


    else:

        logger.info(
            "Ignorado: empate_pontuacao | %s",
            detalhes
        )

        return {
            "direcao": None,
            "confianca": 0.0,
            "motivo_ignorado": "empate_pontuacao",
            "detalhes": detalhes
        }



    detalhes["confianca_final"] = confianca



    # ==========================
    # Entrada mínima
    # ==========================

    if confianca < confianca_minima:

        logger.info(
            "Ignorado: pontuacao_baixa %s | %s",
            confianca,
            detalhes
        )


        return {
            "direcao": None,
            "confianca": confianca,
            "motivo_ignorado": "pontuacao_baixa",
            "detalhes": detalhes
        }



    logger.info(
        "SINAL %s | confiança=%s | detalhes=%s",
        direcao,
        confianca,
        detalhes
    )


    return {
        "direcao": direcao,
        "confianca": confianca,
        "motivo_ignorado": None,
        "detalhes": detalhes
    }