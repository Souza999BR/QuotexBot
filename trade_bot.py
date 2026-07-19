# examples/trade_bot.py

import asyncio
import datetime
from pyquotex.config import credentials
from pyquotex.stable_api import Quotex

email, password = credentials()
client = Quotex(
    email=email,
    password=password,
    lang="pt",  # Português
)


async def esperar_proximo_horario():
    """Espera até o próximo múltiplo de 5 minutos (ex: 10:00:00, 10:05:00, etc.)"""
    agora = datetime.datetime.now()
    minutos_restantes = 5 - (agora.minute % 5)
    proxima_vela = agora.replace(second=0, microsecond=0) + datetime.timedelta(minutes=minutos_restantes)
    segundos_ate_proxima = (proxima_vela - agora).total_seconds()

    print(f"\n⏳ Aguardando até {proxima_vela.strftime('%H:%M:%S')} para entrada sincronizada...")
    await asyncio.sleep(segundos_ate_proxima)


async def analise_sentiment(asset_name, duration):
    """Analisa o sentimento do mercado durante a operação"""
    tempo_restante = duration
    while tempo_restante > 0:
        market_mood = await client.get_realtime_sentiment(asset_name)
        sentiment = market_mood.get('sentiment')
        if sentiment:
            print(f"\rSell: {sentiment.get('sell')}%  |  Buy: {sentiment.get('buy')}%", end="")
        await asyncio.sleep(5)
        tempo_restante -= 5


async def calculate_profit(asset_name, amount, balance):
    """Calcula lucro baseado no payout do ativo"""
    payout = client.get_payout_by_asset(asset_name)
    profit = ((payout / 100) * amount)
    balance += amount + profit
    return balance, profit


async def martingale_apply(amount, asset_name, direction, duration, balance, martingale_quantity):
    """Aplica Martingale em caso de perda"""
    while martingale_quantity > 0:
        balance -= amount
        print(f"\n[Martingale] Entrando com {amount} em {asset_name} ({direction}) por {duration}s")
        status, buy_info = await client.buy(amount, asset_name, direction, duration)

        if not status:
            print("ERRO: Falha ao abrir operação no Martingale.")
            return balance, 0, False

        await analise_sentiment(asset_name, duration)
        result = await check_result(buy_info, direction)

        if result == "Win":
            balance, profit = await calculate_profit(asset_name, amount, balance)
            return balance, profit, True
        elif result == "Doji":
            print("Resultado: DOJI (sem lucro ou perda).")
            return balance, 0, True

        amount *= 2
        martingale_quantity -= 1

    print("❌ Martingale esgotado (perda total).")
    return balance, 0, False


async def check_result(buy_data, direction):
    """Verifica resultado da operação"""
    open_price = buy_data.get('openPrice')

    while True:
        prices = await client.get_realtime_price(buy_data['asset'])
        if not prices:
            continue

        current_price = prices[-1]['price']
        print(f"\nPreço Atual: {current_price:.5f} | Abertura: {open_price:.5f}")

        if (direction == "call" and current_price > open_price) or (
            direction == "put" and current_price < open_price):
            print("✅ Resultado: WIN")
            return 'Win'
        elif current_price == open_price:
            print("⚪ Resultado: DOJI")
            return 'Doji'
        else:
            print("❌ Resultado: LOSS")
            return 'Loss'


async def trade_and_monitor():
    """Loop principal do bot"""
    check_connect, message = await client.connect()
    if not check_connect:
        print("❌ Falha ao conectar:", message)
        return

    amount = 50
    asset = "AUDCAD"
    direction = "call"       # Pode mudar para "put"
    duration = 300           # ⏱️ 5 minutos (300s)
    balance = await client.get_balance()
    initial_balance = balance
    martingale_quantity = 2

    print(f"💰 Saldo inicial: {balance}")
    asset_name, asset_data = await client.get_available_asset(asset, force_open=True)

    if not asset_data[2]:
        print("❌ Ativo fechado.")
        return

    print("✅ Ativo aberto, iniciando operação sincronizada com o relógio do PC...")

    while True:
        # 🕒 Espera o próximo múltiplo de 5 minutos
        await esperar_proximo_horario()

        if not await client.check_connect():
            await client.connect()

        print(f"\n{'=' * 100}")
        print(f"🚀 Entrando em {asset_name} ({direction}) - duração {duration}s - valor {amount}")

        status, buy_info = await client.buy(amount, asset_name, direction, duration)
        if not status:
            print("❌ Falha ao abrir operação, tentando novamente na próxima vela.")
            continue

        balance -= amount
        print(f"Novo saldo: {balance}")

        await analise_sentiment(asset_name, duration)
        result = await check_result(buy_info, direction)

        if result == "Win":
            balance, profit = await calculate_profit(asset_name, amount, balance)
            print(f"✅ Lucro: {profit:.2f} | Novo saldo: {balance:.2f}")
        elif result == "Doji":
            print("⚪ Nenhum lucro ou prejuízo (Doji).")
        else:
            balance, profit, success = await martingale_apply(
                amount * 2,
                asset_name,
                direction,
                duration,
                balance,
                martingale_quantity
            )
            if success:
                print(f"✅ Lucro após Martingale: {profit:.2f}")
            else:
                print(f"❌ Perda acumulada: {initial_balance - balance:.2f}")

        print(f"\nAguardando próxima vela de 5 minutos...\n")


async def main():
    await trade_and_monitor()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\n🛑 Encerrando o programa.")
    finally:
        loop.close()
