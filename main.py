import time
import logging
from bs4 import BeautifulSoup as bs
from threading import Event
import os

import models
import alert
import core
import asyncio
import httpx

from tortoise import Tortoise, run_async
from tortoise.functions import Count
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiogram.utils.formatting import Text
from aiogram import exceptions
import xtras
from router import router as start_router

os.system("title pocketoption [%s]" % core.email)

DEBUG = True
core.chat_ids = core.load_chatids()

logger: logging.Logger = core.logger
models.logger = logger
alert.logger = logger

import sys
print("sys.platform: ", sys.platform)
if sys.platform == 'win32':
    # Configure StreamHandler to use utf-8 encoding
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.handlers = [console_handler] 

output_format = "%A, %B %d, %Y"

# Telegram Bot Initialization
bot = Bot(token=core.bot_token)
dp = Dispatcher()


# Broadcast event
BROADCAST_EVENT = Event()
BROADCAST_EVENT.set()

# Withdrawal event
WITHDRAWAL_EVENT = Event()
WITHDRAWAL_EVENT.set()


TELEGRAM_MESSAGE_INTERVAL = 0.5  # <- in seconds
RETRIEVAL_INTERVAL = 1

periods = [
    # "Total",
    "Current week"
]


async def db_init():
    await Tortoise.init(
        db_url="sqlite://%s" % models.db_name,
        modules={"models": ["models"]}
    )
    await Tortoise.generate_schemas()
    if not await models.Withdrawal.first():
        # Default withdrawal setting
        logger.debug("Creating default setting for Auto-withdrawal")
        await models.Withdrawal(**{
            "auto": False,
            "auto_all": True
        }).save()


async def db_close():
    await Tortoise.close_connections()

async def fetch(url: str, **kwargs) -> httpx.Response:
    try:
        return await core.session.get(url, **kwargs)
    finally:
        core.save_cookies(core.session)


def generate_otp_payload() -> dict:
    otp = core.get_auth_code()
    return {
        "one_time_password": "%s %s" % (otp[:3], otp[3:])
    }


def generate_payment_payload(data: bs, _type: str, balance: int | float) -> dict:
    payload = {
        "_token": data.select_one('[name="_token"]').get("value"),
        "_method": "POST",
        "amount": str(balance),
        # "balance_type": _type.lower() == "balance" and "balance" or "bonus_balance",
        "credit": "0",
        "method": "18",
        "user_data[100][uid]": "",
        "user_data[100][uids]": "",
    }
    
    if data.select_one('input[name="one_time_password"]'):
        payload.update(generate_otp_payload())

    return payload

async def get_recaptcha_code() -> str:
    loop = asyncio.get_running_loop()
    # result = await loop.run_in_executor(None, lambda: core.solver.recaptcha(
    #     sitekey="6LeF_OQeAAAAAMl5ATxF48du4l-4xmlvncSUXGKR",
    #     url=core.login_link
    # ))
    result = await loop.run_in_executor(None, lambda: core.solver.solve_captcha(
        site_key="6LeF_OQeAAAAAMl5ATxF48du4l-4xmlvncSUXGKR",
        page_url=core.login_link
    ))
    print("code: ", result)
    return result

async def generate_login_payload(data: bs, otp_verify: bool = False) -> dict:
    payload = {
        "_token": data.select_one('[name="_token"]').get("value"),
        "email": core.email,
        "password": core.password,
    }
    
    print("payload", payload)
    print("hhh", generate_otp_payload())
    

    if otp_verify:
        payload.update(generate_otp_payload())
    else:
        payload.update({
            "g-recaptcha-response": await get_recaptcha_code()
        })

    return payload


def validate_login(res:  httpx.Response) -> bool:
    return res is not None and res.url == core.logged_in_link or False


def validate_amount(amount: int | float) -> int | float:
    return bool(amount and amount >= 11) and amount or None


def calculate_pool_value(deposits: float, withdrawals: float, hold: float) -> float:
    return round((float(deposits-withdrawals)*0.7)-hold, 2)


async def save_statistics_log(period: str, data: dict) -> None:
    if period != "Current week":
        return

    try:
        io_log_obj = models.StatisticsLog(**{
            "period": period,
            "account_status": data["account_status"],
            "deposits": data["deposits_current"],
            "commission": data["commission_current"],
            "withdrawals": data["withdrawals_current"],
            "hold": data["hold_current"],
            "pool": data["pool_current"],
            "balance": data["balance_current"],
            "bonus": data["bonus_current"],
            "visitors": data["visitors"],
            "registrations": data["registrations"],
            "registrations_avg": data["registrations_avg"],
            "ftd": data["ftd"],
            "ftd_avg": data["ftd_avg"],
        })
        await io_log_obj.save()
    except Exception as e:
        logger.exception("ERR_SAVE_STATITICS_LOG: %s | %s" % (
            period, e
        ))

async def get_last_week_data() -> models.StatisticsLog | None:
    # Get exactly one week ago at the same hour
    one_week_ago = datetime.now() - timedelta(days=7)
    one_week_ago = one_week_ago.replace(minute=0, second=0, microsecond=0)
    
    # Try to get data from that exact hour or the latest before it
    last_week_data = await models.StatisticsLog.filter(
        updated__lte=one_week_ago
    ).order_by('-updated').first()
    
    if last_week_data:
        logger.debug(f"Found data from: {last_week_data.updated}")
    else:
        logger.debug("No historical data found")
    
    return last_week_data

async def process_statistics(period: str, account_status: str = None, update_db: bool = True, failsafe: bool = False) -> dict:
    data = {}
    try:
        res_statistics = await fetch(
            url=period == "Total" and core.statistics_link or core.statistics_current_week_link,
            headers=core.report_headers
        )
        # logger.debug("Response: %s | %s" % (
        #     res_statistics.status_code, res_statistics.url
        # ))

        res_json = res_statistics.json()
        
        # print(res_json)
        if "partnerVisits" in res_json:
            change_in_deposits = 0
            change_in_commission = 0
            change_in_withdrawals = 0
            change_in_hold = 0
            change_in_pool = 0
            change_in_balance = 0
            change_in_bonus = 0

            old_deposits = deposits = res_json["partnerDeposits"]
            old_commission = commission = res_json["partnerCommission"]
            old_withdrawals = withdrawals = res_json["partnerClientsWithdrawals"]
            old_hold = hold = res_json["partnerHoldCommission"]
            old_balance = balance = res_json["partnerBalance"]
            old_bonus = bonus = res_json.get("partnerBonus") or 0.0
            old_pool = pool = calculate_pool_value(
                deposits, withdrawals, hold
            )

            visitors = res_json["partnerVisits"]
            registrations = res_json["partnerClients"]
            ftd = res_json["partnerFTDs"]

            registrations_avg = 0
            ftd_avg = 0
            if visitors:
                registrations_avg = int((registrations/visitors)*100)

            if registrations:
                ftd_avg = round((ftd/registrations)*100, 2)

            io_obj: models.Statistics = None
            if update_db:
                io_obj = await models.Statistics.get_or_none(period=period)
                if not io_obj:
                    io_obj = await models.Statistics.create(
                        period=period,
                        deposits=deposits,
                        old_deposits=deposits,
                        commission=commission,
                        old_commission=commission,
                        withdrawals=withdrawals,
                        old_withdrawals=withdrawals,
                        hold=hold,
                        old_hold=hold,
                        pool=pool,
                        old_pool=pool,
                        balance=balance,
                        old_balance=balance,
                        bonus=bonus,
                        old_bonus=bonus,
                        account_status=account_status
                    )
                    await io_obj.save()
                    
                    old_deposits = 0
                    old_commission = 0
                    old_withdrawals = 0
                    old_hold = 0
                    old_pool = 0
                    old_balance = 0
                    old_bonus = 0
                else:
                    old_deposits = io_obj.deposits
                    old_commission = io_obj.commission
                    old_withdrawals = io_obj.withdrawals
                    old_hold = io_obj.hold
                    old_pool = io_obj.pool
                    old_balance = io_obj.balance
                    old_bonus = io_obj.bonus

            # print("io_obj.deposits: ", old_deposits)
            # print("io_obj.commission: ", old_commission)
            # print("io_obj.withdrawals: ", old_withdrawals)
            # print("io_obj.hold: ", old_hold)
            # print("io_obj.pool: ", old_pool)
            # print("io_obj.balance: ", old_balance)
            # print("io_obj.bonus: ", old_bonus)
            
            # print(res_json)

            change_in_deposits = round(deposits - old_deposits, 2)
            change_in_commission = round(commission - old_commission, 2)
            change_in_withdrawals = round(withdrawals - old_withdrawals, 2)
            change_in_hold = round(hold - old_hold, 2)
            change_in_pool = round(pool - old_pool, 2)
            change_in_balance = round(balance - old_balance, 2)
            change_in_bonus = round(bonus - old_bonus, 2)

            data.update({
                "deposits_old": old_deposits,
                "deposits_change": change_in_deposits,
                "deposits_current": deposits,

                "commission_old": old_commission,
                "commission_change": change_in_commission,
                "commission_current": commission,

                "withdrawals_old": old_withdrawals,
                "withdrawals_change": change_in_withdrawals,
                "withdrawals_current": withdrawals,

                "hold_old": old_hold,
                "hold_change": change_in_hold,
                "hold_current": hold,

                "pool_old": old_pool,
                "pool_change": change_in_pool,
                "pool_current": pool,

                "balance_old": old_balance,
                "balance_change": change_in_balance,
                "balance_current": balance,

                "bonus_old": old_bonus,
                "bonus_change": change_in_bonus,
                "bonus_current": bonus,

                "visitors": visitors,
                "registrations": registrations,
                "ftd": ftd,
                "registrations_avg": registrations_avg,
                "ftd_avg": ftd_avg,
                "account_status": account_status
            })

            if io_obj is not None:
                io_obj.deposits = deposits
                io_obj.commission = commission
                io_obj.withdrawals = withdrawals
                io_obj.hold = hold
                io_obj.pool = pool
                io_obj.balance = balance
                io_obj.bonus = bonus
                io_obj.account_status = account_status
                io_obj.old_deposits = old_deposits
                io_obj.old_commission = old_commission
                io_obj.old_withdrawals = old_withdrawals
                io_obj.old_hold = old_hold
                io_obj.old_pool = old_pool
                io_obj.old_balance = old_balance
                io_obj.old_bonus = old_bonus
                await io_obj.save()

        # print("io_obj: ", io_obj)
    except Exception as e:
        if failsafe:
            logger.exception("ERR_PROCESS_SUMMARY -> Period: %s -> Error: %s" % (
                period, e
            ))
        else:
            await asyncio.sleep(2)
            return await process_statistics(period, account_status, update_db=update_db, failsafe=True)
    else:
        if update_db:
            await save_statistics_log(period, data)
            logger.debug("Processed -> Statistics -> %s" % period.capitalize())

    io_log_obj = models.StatisticsLog(**{
        "period": period,
        "account_status": data["account_status"],
        "deposits": data["deposits_current"],
        "commission": data["commission_current"],
        "withdrawals": data["withdrawals_current"],
        "hold": data["hold_current"],
        "pool": data["pool_current"],
        "balance": data["balance_current"],
        "bonus": data["bonus_current"],
        "visitors": data["visitors"],
        "registrations": data["registrations"],
        "registrations_avg": data["registrations_avg"],
        "ftd": data["ftd"],
        "ftd_avg": data["ftd_avg"],
    })
    
    last_week_data = await get_last_week_data()
    if not last_week_data:
        last_week_data = io_log_obj
    # print("last_week_data-deposits: ", last_week_data.deposits)
    # print("last_week_data-commission: ", last_week_data.commission)
    # print("last_week_data-withdrawals: ", last_week_data.withdrawals)
    # print("last_week_data-hold: ", last_week_data.hold)
    # print("last_week_data-pool: ", last_week_data.pool)
    # print("last_week_data-balance: ", last_week_data.balance)
    # print("last_week_data-bonus: ", last_week_data.bonus)
    # print("last_week_data-visitors: ", last_week_data.visitors)
    # print("last_week_data-registrations: ", last_week_data.registrations)
    # print("last_week_data-registrations_avg: ", last_week_data.registrations_avg)
    # print("last_week_data-ftd: ", last_week_data.ftd)
    # print("last_week_data-ftd_avg: ", last_week_data.ftd_avg)
    # print("last_week_data-account_status: ", last_week_data.account_status)
    # print("last_week_data-period: ", last_week_data.period)
    # print("last_week_data-updated: ", last_week_data.updated)
    compared_data = format_comparison(last_week_data, io_log_obj, "time", data)
    # print("compared_data: ", compared_data)
    return compared_data


def format_only_change(stats: dict, period: str) -> str:
    final_message = "\n\n".join([
        message.strip()
        for message in [
            alert.formatted_message(
                "hold", stats["hold_old"], stats["hold_change"], stats["hold_current"], stats["week_change_in_hold"],
            ),
            alert.formatted_message(
                "deposits", stats["deposits_old"], stats["deposits_change"], stats["deposits_current"], stats["week_change_in_deposits"],
            ),
            alert.formatted_message(
                "withdrawals", stats["withdrawals_old"], stats["withdrawals_change"], stats["withdrawals_current"], stats["week_change_in_withdrawals"],
            ),
            alert.formatted_message(
                "commission",  stats["commission_old"], stats["commission_change"], stats["commission_current"], stats["week_change_in_commission"],
            ),
            alert.formatted_message(
                "pool", stats["pool_old"], stats["pool_change"], stats["pool_current"], stats["week_change_in_pool"],
            ),
            alert.formatted_message(
                "balance", stats["balance_old"], stats["balance_change"], stats["balance_current"], stats["week_change_in_balance"],
            ),
            alert.formatted_message(
                "bonus", stats["bonus_old"], stats["bonus_change"], stats["bonus_current"], stats["week_change_in_bonus"],
            )
        ]
        if message and message.strip()
    ])

    if final_message:
        final_message += "\n\n" + alert.formatted_message(
            "bottom", stats["visitors"], stats["registrations"], stats["registrations_avg"], stats["ftd"], stats["ftd_avg"],
            stats["week_change_in_visitors"], stats["week_change_in_registrations"], stats["week_change_in_registrations_avg"], stats["week_change_in_ftd"], stats["week_change_in_ftd_avg"]
        )
        final_message += "\n\n⚙️ Account Status: %s\n\n📅 %s" % (stats["account_status"], period)

    return final_message

def format_even_no_change(stats: dict, period: str) -> str:
    final_message = "\n\n".join([
        message.strip()
        for message in [
            alert.formatted_message_even_no_change(
                "hold", stats["hold_old"], stats["hold_change"], stats["hold_current"], stats["week_change_in_hold"],
            ),
            alert.formatted_message_even_no_change(
                "deposits", stats["deposits_old"], stats["deposits_change"], stats["deposits_current"], stats["week_change_in_deposits"],
            ),
            alert.formatted_message_even_no_change(
                "withdrawals", stats["withdrawals_old"], stats["withdrawals_change"], stats["withdrawals_current"], stats["week_change_in_withdrawals"],
            ),
            alert.formatted_message_even_no_change(
                "commission",  stats["commission_old"], stats["commission_change"], stats["commission_current"], stats["week_change_in_commission"],
            ),
            alert.formatted_message_even_no_change(
                "pool", stats["pool_old"], stats["pool_change"], stats["pool_current"], stats["week_change_in_pool"],
            ),
            alert.formatted_message_even_no_change(
                "balance", stats["balance_old"], stats["balance_change"], stats["balance_current"], stats["week_change_in_balance"],
            ),
            alert.formatted_message_even_no_change(
                "bonus", stats["bonus_old"], stats["bonus_change"], stats["bonus_current"], stats["week_change_in_bonus"],
            )
        ]
        if message and message.strip()
    ])

    if final_message:
        final_message += "\n\n" + alert.formatted_message_even_no_change(
            "bottom", stats["visitors"], stats["registrations"], stats["registrations_avg"], stats["ftd"], stats["ftd_avg"],
            stats["week_change_in_visitors"], stats["week_change_in_registrations"], stats["week_change_in_registrations_avg"], stats["week_change_in_ftd"], stats["week_change_in_ftd_avg"]
        )
        final_message += "\n\n⚙️ Account Status: %s\n\n📅 %s" % (stats["account_status"], period)

    return final_message


def format_comparison(previous_obj: models.StatisticsLog, current_obj: models.StatisticsLog, filter: str, data: dict) -> str:
    change_in_deposits = round(current_obj.deposits - previous_obj.deposits, 2)
    change_in_commission = round(
        current_obj.commission - previous_obj.commission, 2)
    change_in_withdrawals = round(
        current_obj.withdrawals - previous_obj.withdrawals, 2)
    change_in_hold = round(current_obj.hold - previous_obj.hold, 2)
    change_in_pool = round(current_obj.pool - previous_obj.pool, 2)
    change_in_balance = round(current_obj.balance - previous_obj.balance, 2)
    change_in_bonus = round(current_obj.bonus - previous_obj.bonus, 2)
    change_in_visitors = round(current_obj.visitors - previous_obj.visitors, 2)
    change_in_registrations = round(
        current_obj.registrations - previous_obj.registrations, 2)
    change_in_registrations_avg = round(
        current_obj.registrations_avg - previous_obj.registrations_avg, 2)
    change_in_ftd = round(current_obj.ftd - previous_obj.ftd, 2)
    change_in_ftd_avg = round(current_obj.ftd_avg - previous_obj.ftd_avg, 2)

    period = "Compared last week (%s)" % (
        filter == "time" and "Time" or "Day"
    )
    
    data["week_change_in_deposits"] = change_in_deposits
    data["week_change_in_commission"] = change_in_commission
    data["week_change_in_withdrawals"] = change_in_withdrawals
    data["week_change_in_hold"] = change_in_hold
    data["week_change_in_pool"] = change_in_pool
    data["week_change_in_balance"] = change_in_balance
    data["week_change_in_bonus"] = change_in_bonus
    data["week_change_in_visitors"] = change_in_visitors
    data["week_change_in_registrations"] = change_in_registrations
    data["week_change_in_registrations_avg"] = change_in_registrations_avg
    data["week_change_in_ftd"] = change_in_ftd
    data["week_change_in_ftd_avg"] = change_in_ftd_avg
    
    return data

    # return "\n\n".join([
    #     message
    #     for message in [
    #         alert.formatted_message_compare(
    #             "hold", previous_obj.hold, change_in_hold, current_obj.hold,
    #         ),
    #         alert.formatted_message_compare(
    #             "deposits", previous_obj.deposits, change_in_deposits, current_obj.deposits,
    #         ),
    #         alert.formatted_message_compare(
    #             "withdrawals", previous_obj.withdrawals, change_in_withdrawals, current_obj.withdrawals,
    #         ),
    #         # alert.formatted_message_compare(
    #         #     "commission", previous_obj.commission, change_in_commission, current_obj.commission,
    #         # ),
    #         alert.formatted_message_compare(
    #             "pool", previous_obj.pool, change_in_pool, current_obj.pool,
    #         ),
    #         # alert.formatted_message_compare(
    #         #     "balance", previous_obj.balance, change_in_balance, current_obj.balance,
    #         # ),
    #         # alert.formatted_message_compare(
    #         #     "bonus", previous_obj.bonus, change_in_bonus, current_obj.bonus,
    #         # ),
    #         "\n".join(alert.mapping["bottom"]) % (
    #             alert.format_change(int(change_in_visitors)),
    #             alert.format_change(int(change_in_registrations)),
    #             alert.format_percentage_change(change_in_registrations_avg),
    #             alert.format_change(int(change_in_ftd)),
    #             alert.format_percentage_change(change_in_ftd_avg),
    #         ),
    #     ]
    # ]).replace("Income: ", "Difference: ").replace("Outcome: ", "Difference: ")\
    #     .replace("$-", "-$").strip() + str("\n\n📅 %s" % period)


def format_no_change(stats: dict, period: str) -> str:
    return "\n".join([
        message
        for message in [
            alert.formatted_message_current(
                "hold", stats["hold_old"], stats["hold_change"], stats["hold_current"],
            ),
            alert.formatted_message_current(
                "deposits", stats["deposits_old"], stats["deposits_change"], stats["deposits_current"],
            ),
            alert.formatted_message_current(
                "withdrawals", stats["withdrawals_old"], stats["withdrawals_change"], stats["withdrawals_current"],
            ),
            alert.formatted_message_current(
                "commission",  stats["commission_old"], stats["commission_change"], stats["commission_current"],
            ),
            alert.formatted_message_current(
                "pool", stats["pool_old"], stats["pool_change"], stats["pool_current"],
            ),
            alert.formatted_message_current(
                "balance", stats["balance_old"], stats["balance_change"], stats["balance_current"],
            ),
            alert.formatted_message_current(
                "bonus", stats["bonus_old"], stats["bonus_change"], stats["bonus_current"],
            ),
            alert.formatted_message_current(
                "bottom", stats["visitors"], stats["registrations"], stats["registrations_avg"], stats["ftd"], stats["ftd_avg"],
            ),
            "\n\n📅 %s\n\n/comparetime" % period
        ]
    ]).strip()


def format_withdrawal(_type: str, amount: int | float, mode: str = "Bot", wallet_str: str = "") -> str:
    # import locale

    # locale.setlocale(locale.LC_ALL, 'en_US.UTF-8')
    # print("atof: ", locale.atof(str(amount).replace("'", "").strip().split("\n", 1)[0].strip()))

    return "\n".join([
        "🏧 Withdrawal requested",
        "ℹ️ Request initiated: %s" % mode,
        # "ℹ️ Balance type: %s" % _type.capitalize(),
        # "💲 Amount: $%s\n" % round(float(str(amount).replace("'", "").strip().split("\n", 1)[0].strip()), 2),
        "💲 Amount: $%s\n" % str(amount).replace("'", "").strip().split("\n", 1)[0].strip(),
        "ℹ️ Payment method: 🏦 Wallet",
        "=========================",
        wallet_str
    ]).strip()


async def get_statistics() -> dict[str, dict]:
    starting_time = time.time()
    final_info = {}

    account_status = await perform_login()
    try:
        # Looping on periods to process reports
        for period in periods:
            stats = await process_statistics(period, account_status)
            if stats:
                final_info.update({
                    period: stats
                })
    except Exception as e:
        logger.exception("ERR_GET_STATISTICS: %s" % e)
    finally:
        logger.debug(
            "Total time taken to perform the task: %s seconds" %
            round(time.time() - starting_time, 2)
        )
    return final_info


def send_alert() -> None:
    messages = core.load_messages()
    if messages:
        chat_ids = core.load_chatids()
        # In case of failure during loading latest chatids for unknown reason,
        # it will use the previously loaded chatids in starting of the script
        if not chat_ids:
            chat_ids = core.chat_ids

        for chat_id in chat_ids:
            for message in messages:
                _ = alert.send_message(
                    bot_token=core.bot_token,
                    chat_id=chat_id,
                    message=core.fix_message_format(message)
                )
    else:
        logger.debug("No reports were processed!!")


async def perform_login() -> None:
    # Loading Old Session cookies
    core.cookies = core.load_cookies()

    IS_LOGGED_IN = False
    if core.cookies:
        core.session.cookies.update(core.cookies)
        try:
            res = await core.session.get(core.logged_in_link, timeout=10)
        except:
            res = None

        if IS_LOGGED_IN := validate_login(res):
            logger.debug("Old session worked fine.")
            data = bs(res.text, "lxml")
            status_span = data.find('span', class_='status-block-color')
            account_status = status_span.text.strip() if status_span else None
            print("Account Status:", account_status)
            return account_status
            
        else:
            logger.debug("Old Session expired!! Trying to login again..")
            core.session.cookies.clear()

    if not IS_LOGGED_IN:
        res = await core.session.get(url=core.home_link)

        # logger.debug("Response: %s | %s" % (
        #     res.status_code, res.url
        # ))

        res_l = await core.session.post(url=core.login_link, data=await generate_login_payload(
            data=bs(res.text, "lxml")
        ))

        # logger.debug("Response: %s | %s" % (
        #     res_l.status_code, res_l.url
        # ))

        if 'name="one_time_password"' in res_l.text:
            res_l = await core.session.post(url=core.otp_verify_link, data=await generate_login_payload(
                data=bs(res_l.text, "lxml"), otp_verify=True
            ))

            # logger.debug("Response: %s | %s" % (
            #     res_l.status_code, res_l.url
            # ))

        if validate_login(res_l):
            logger.debug("Logged-In successfully!")
            core.save_cookies(core.session)
            data = bs(res_l.text, "lxml")
            status_span = data.find('span', class_='status-block-color')
            account_status = status_span.text.strip() if status_span else None
            print("Account Status:", account_status)
            return account_status


def validate_minute(minute: int) -> bool:
    return datetime.now(tz=models.pytz.utc).time().minute == minute

def validate_minute_withdrawal() -> bool:
    current_minute = datetime.now(tz=models.pytz.utc).time().minute
    # return current_minute % 5 == 0
    return True

def get_error(res: httpx.Response) -> str:
    data = bs(res.text, "lxml")
    return "\n".join([
        "%s: %s" % (
            div.select_one("strong").text.strip(),
            div.select_one("ul li").text.strip()
        )
        for div in data.select("div.alert-danger")
    ])


async def send_message(user_id: int, text: str, disable_notification: bool = False, **kwargs) -> bool:
    """
    Safe messages sender
    :param user_id:
    :param text:
    :param disable_notification:
    :return:
    """
    try:
        if "parse_down" not in kwargs:
            kwargs["parse_mode"] = 'Markdown'

        await bot.send_message(user_id, text, disable_notification=disable_notification, disable_web_page_preview=True, **kwargs)
    except exceptions.TelegramBadRequest as e:
        print(e)
        if "chat not found" in str(e):
            logger.error(f"Target [ID:{user_id}]: invalid user ID")
        elif "user is deactivated" in str(e):
            logger.error(f"Target [ID:{user_id}]: user is deactivated")
        elif "bot was blocked" in str(e):
            logger.error(f"Target [ID:{user_id}]: blocked by user")
        else:
            logger.error(f"Target [ID:{user_id}]: {str(e)}")
    except exceptions.TelegramRetryAfter as e:
        logger.error(
            f"Target [ID:{user_id}]: Flood limit is exceeded. Sleep {e.retry_after} seconds.")
        await asyncio.sleep(e.retry_after)
        # Recursive call
        return await send_message(user_id, text, disable_notification, **kwargs)
    except exceptions.TelegramAPIError:
        logger.exception(f"Target [ID:{user_id}]: failed")
    else:
        logger.info(f"Target [ID:{user_id}]: success")
        return True
    return False


async def test_func():
    current_stats = await get_statistics()
    print("current_stats: ", current_stats)
    core.chat_ids = core.load_chatids()
    print(core.chat_ids)
    for period in current_stats:
        stats = current_stats[period]
        # print("stats: ", stats)
        # print("processed_message: ", format_only_change(stats, period))
        if processed_message := format_even_no_change(stats, period):
            for chat_id in core.chat_ids:
                print("chat_id: ", chat_id)
                await send_message(chat_id, text=core.fix_message_format(processed_message))
                # print(processed_message)
                # print(core.fix_message_format(processed_message))
    
async def broadcast(message: types.Message = None) -> None:
    if message:
        await message.reply("Broadcast *Started!*", parse_mode='Markdown')
        logger.info("Target [%s]: BROADCAST STARTED!" % message.chat.id)

    current_stats = {}
    PROCESSED = False
    ALERT_SENT = False
    await test_func()
    # return
    while BROADCAST_EVENT.is_set():
        # print("BROADCAST_EVENT.is_set(): ", BROADCAST_EVENT.is_set())
        try:
            if not PROCESSED:
                if validate_minute(59):
                    current_stats = await get_statistics()
                    PROCESSED = True
                else:
                    continue
            else:
                if not isinstance(current_stats, dict) or not current_stats:
                    current_stats = await get_statistics()

            if not ALERT_SENT:
                if validate_minute(0):
                    ALERT_SENT = True
                    core.chat_ids = core.load_chatids()
                    for period in current_stats:
                        stats = current_stats[period]
                        if processed_message := format_only_change(stats, period):
                            for chat_id in core.chat_ids:
                                await send_message(chat_id, text=core.fix_message_format(processed_message))
                        else:
                            logger.debug("No change detected!!")
                else:
                    continue

            if PROCESSED and ALERT_SENT:
                if validate_minute(1):
                    current_stats = {}
                    PROCESSED = False
                    ALERT_SENT = False

        except Exception as e:
            logger.exception("ERR_BROADCAST: %s" % e)
        finally:
            await asyncio.sleep(1)

async def verify_payment(amount: int | float, res: httpx.Response = None, failsafe: bool = False) -> bool:
    try:
        if res is None:
            res = await fetch(url=core.payment_history_link)
            logger.debug("Response: %s | %s" % (
                res.status_code, res.url
            ))

        data_h = bs(res.text, "lxml")
        if td := data_h.select_one('#panel-1 td[data-label="Amount, $"]'):
            td_value = td.text.replace("$", "").replace("'", "").replace(",", "").strip()
            try:
                td_value = int(float(td_value)) if isinstance(amount, int) else float(td_value)
                if td_value == amount:
                    logger.debug(
                        "PROCESS_WITHDRAWAL_VERIFICATION -> SUCCESS -> %s" % amount)
                    return True

            except Exception as e:
                logger.debug("INVALID AMOUNT STRING: %s" % td_value)
        else:
            logger.debug(
                "WARN_PROCESS_WITHDRAWAL -> TABLE_NOT_FOUND: %s (%s)" %
                (res.status_code, res.url)
            )
    except Exception as e:
        logger.exception(
            "ERR_VERIFY_PAYMENT: %s (%s) | Failsafe: %s" %
            (res.status_code, res.url, failsafe)
        )

    if not failsafe:
        await asyncio.sleep(5)
        return await verify_payment(amount, failsafe=True)

    logger.debug("PROCESS_WITHDRAWAL_VERIFICATION -> FAILED -> %s" % amount)
    
async def verify_payment_tmp(amount: int | float, res, failsafe: bool = False) -> bool:
    try:

        data_h = bs(res, "lxml")
        if td := data_h.select_one('#panel-1 td[data-label="Amount, $"]'):
            td_value = td.text.replace("$", "").replace("'", "").replace(",", "").strip()
            try:
                td_value = int(float(td_value)) if isinstance(amount, int) else float(td_value)
                if td_value == amount:
                    logger.debug(
                        "PROCESS_WITHDRAWAL_VERIFICATION -> SUCCESS -> %s" % amount)
                    return True

            except Exception as e:
                logger.debug("INVALID AMOUNT STRING: %s" % td_value)
        else:
            pass
    except Exception as e:
        print(e)

    if not failsafe:
        await asyncio.sleep(5)
        return await verify_payment(amount, failsafe=True)

    logger.debug("PROCESS_WITHDRAWAL_VERIFICATION -> FAILED -> %s" % amount)


async def process_withdrawal(_type: str, amount: int | float) -> bool:
    if amount > 10:
        return False
    await perform_login()
    payment_payload = {}
    try:
        res_r = await fetch(url=core.payment_request_link)
        logger.debug("Response: %s | %s" % (
            res_r.status_code, res_r.url
        ))
        payment_payload = generate_payment_payload(
            data=bs(res_r.text, "lxml"), _type=_type, balance=amount
        )

        res_post_r = await core.session.post(
            url=core.payment_request_link,
            data=payment_payload,
        )
        logger.debug("Response: %s | %s" % (
            res_post_r.status_code, res_post_r.url
        ))
        # with open("withdrawal_request.html", "wb") as f:
        #     f.write(res_post_r.content)
            
        # res_post_r = res_r
        # with open("withdrawal_request.html", "rb") as f:
        #     content = f.read()
            
        # return await verify_payment_tmp(
        #     amount=amount, res=content
        # )
        
        if res_post_r.url == core.payment_history_link:
            return await verify_payment(
                amount=amount, res=res_post_r
            )
        else:
            error = get_error(res_post_r)
            logger.debug(
                "WARN_PROCESS_WITHDRAWAL: %s (%s) (%s) (%s) -> %s" % (
                    res_post_r.status_code, res_post_r.url,
                    str(payment_payload), _type, error
                ))

    except Exception as e:
        logger.exception(
            "ERR_PROCESS_WITHDRAWAL: %s (%s) (%s) | %s" %
            (_type, amount, str(payment_payload), e)
        )

async def get_wallet_str(amount: float) -> str:
    wallet_info = ""
    try:
        res = await fetch(core.payment_history_link)
        data = bs(res.text, "lxml")
        for tr in data.select("#panel-1 tr"):
            if not tr.select_one("td"):
                continue
            if td_element := tr.select_one('td[data-label="Amount, $"]'):
                amount_str = td_element.text.replace("$", "").replace(",", "").strip()
                # print("amount_str: ", amount_str)
                # print("amount: ", amount)
                if str(amount) in amount_str:
                    wallet_info = tr.select_one(
                        '[data-label="Payment method"]').text.strip()
                    break
        # print("wallet_info: ", wallet_info)
    except Exception as e:
        logger.exception("ERR_GET_WALLET_STR: %s" % amount)

    return wallet_info


async def get_latest_payment_requests(last_request_id: str) -> list[str, list[dict]]:
    records = []
    new_request_id = last_request_id
    try:
        res = await fetch(core.payment_history_link)
        data = bs(res.text, "lxml")
        # print("data: ", data)
        for tr in data.select("#panel-1 tr"):
            if not tr.select_one("td"):
                continue

            if id_element := tr.select_one('[data-label="ID"]'):
                if id_element.text.strip() == last_request_id:
                    break

                records.append({
                    "ID": tr.select_one('[data-label="ID"]').text.strip(),
                    "Amount, $": tr.select_one('[data-label="Amount, $"]').text.replace("$", "").strip(),
                    "Payment method": tr.select_one('[data-label="Payment method"]').text.strip(),
                })

    except Exception as e:
        logger.exception(
            "ERR_GET_LATEST_PAYMENT_REQUEST: %s" %
            last_request_id
        )

    if records:
        new_request_id = records[0]["ID"]

    return new_request_id, records


async def get_last_payment_request_id() -> str:
    request_id = ""
    try:
        res = await fetch(core.payment_history_link)
        data = bs(res.text, "lxml")
        if id_element := data.select_one('#panel-1 tr td[data-label="ID"]'):
            request_id = id_element.text.strip()

    except Exception as e:
        logger.exception("ERR_GET_LAST_PAYMENT_REQUEST_ID")

    return request_id


def save_withdrawal_message(message: str) -> None:
    with open("last_withdrawal_message.txt", "w", encoding="utf-8") as f:
        f.write(message)
    try:
        logger.debug(message)
    except Exception as e:
        pass

async def monitor_test(history_obj):
    new_request_id, new_requests = await get_latest_payment_requests("1234567890")
    # print("new_request_id: ", new_request_id)
    if history_obj.request_id and new_requests:
        for request in new_requests:
            print(request["Amount, $"])
            processed_message = format_withdrawal(
                _type="---",
                amount=request["Amount, $"],
                mode="Manual",
                wallet_str=request["Payment method"]
            )
            # print("fixed_message: ", core.fix_message_format(processed_message))
            core.chat_ids = core.load_chatids()
            for chat_id in core.chat_ids:
                await send_message(
                    chat_id,
                    text=core.fix_message_format(
                        processed_message)
                )
            try:
                logger.debug(processed_message)
            except:
                print(processed_message)
    logger.debug("First Check -> Latest ID: %s | Existing ID: %s" % (
        new_request_id, history_obj.request_id
    ))
    if new_request_id and history_obj.request_id != new_request_id:
        history_obj.request_id = new_request_id
        await history_obj.save()
    
    # if await models.is_auto_withdrawal_active():
    current_stats = await process_statistics(period="Current week", update_db=False)
    print("current_stats: ", current_stats)
    for key in ["Balance", "Bonus"]:
        amount = current_stats.get("%s_current" % key.lower())
        if not amount or int(amount) < 11:
            logger.debug("%s -> %s -> Not enough for Withdrawal" % (
                key, str(amount)
            ))
            continue

        amount -= 1
        print("amount: ", amount)
        # payment_status = await process_withdrawal(_type=key, amount=min(10, amount))
        
        # if payment_status:
        #     processed_message = format_withdrawal(
        #         _type=key,
        #         amount=amount,
        #         mode="Bot",
        #         wallet_str=await get_wallet_str(amount)
        #     )
        #     core.chat_ids = core.load_chatids()
        #     for chat_id in core.chat_ids:
        #         await send_message(
        #             chat_id,
        #             text=core.fix_message_format(
        #                 processed_message)
        #         )
        #     save_withdrawal_message(processed_message)
        # else:
        #     logger.debug(
        #         "Auto-Withdrawal request for $%s (%s) failed!!" % (amount, key))
    # else:
    #     logger.debug("Auto-Withdrawal is currently off!!")
        
async def monitor_withdrawal(message: types.Message = None) -> None:
    if message:
        await message.reply("Withdrawal Process *Started!*", parse_mode='Markdown')
        logger.info("Target [%s]: WITHDRAWAL PROCESS STARTED!" %
                    message.chat.id)

    PROCESSED = False
    history_obj: models.History = await models.History.first()
    
    # print("history_obj", history_obj)
    if history_obj is None:
        history_obj = models.History(**{
            "request_id": None
        })
        await history_obj.save()

    logger.debug("Payout Last Request ID: %s" % history_obj.request_id)

    # await monitor_test(history_obj)
    # return
    while WITHDRAWAL_EVENT.is_set():
        print("models.is_auto_withdrawal_active(): ", await models.is_auto_withdrawal_active())
        print("validate_minute_withdrawal(): ", validate_minute_withdrawal())
        try:
            if not PROCESSED:
                if validate_minute_withdrawal():
                    PROCESSED = True
                    # History Check
                    new_request_id, new_requests = await get_latest_payment_requests(history_obj.request_id)
                    if history_obj.request_id and new_requests:
                        for request in new_requests:
                            processed_message = format_withdrawal(
                                _type="---",
                                amount=request["Amount, $"],
                                mode="Manual",
                                wallet_str=request["Payment method"]
                            )
                            core.chat_ids = core.load_chatids()
                            for chat_id in core.chat_ids:
                                await send_message(
                                    chat_id,
                                    text=core.fix_message_format(
                                        processed_message)
                                )
                            try:
                                logger.debug(processed_message)
                            except:
                                print(processed_message)

                    logger.debug("First Check -> Latest ID: %s | Existing ID: %s" % (
                        new_request_id, history_obj.request_id
                    ))
                    if new_request_id and history_obj.request_id != new_request_id:
                        history_obj.request_id = new_request_id
                        await history_obj.save()

                    # Auto-Withdrawal Check
                    if await models.is_auto_withdrawal_active():
                        current_stats = await process_statistics(period="Current week", update_db=False)
                        print("current_stats: ", current_stats)
                        for key in ["Balance", "Bonus"]:
                            amount = current_stats.get("%s_current" % key.lower())
                            if not amount or int(amount) < 11:
                                logger.debug("%s -> %s -> Not enough for Withdrawal" % (
                                    key, str(amount)
                                ))
                                continue

                            amount -= 1
                            amount = min(5, amount)
                            
                            print("amount: ", amount)
                            break
                            
                            payment_status = await process_withdrawal(_type=key, amount=amount)
                            if payment_status:
                                processed_message = format_withdrawal(
                                    _type=key,
                                    amount=amount,
                                    mode="Bot",
                                    wallet_str=await get_wallet_str(amount)
                                )
                                core.chat_ids = core.load_chatids()
                                for chat_id in core.chat_ids:
                                    await send_message(
                                        chat_id,
                                        text=core.fix_message_format(
                                            processed_message)
                                    )
                                save_withdrawal_message(processed_message)
                            else:
                                logger.debug(
                                    "Auto-Withdrawal request for $%s (%s) failed!!" % (amount, key))
                    else:
                        logger.debug("Auto-Withdrawal is currently off!!")

                    # Updating the last payment request id
                    latest_request_id = await get_last_payment_request_id()
                    logger.debug("Second Check -> Latest ID: %s | Existing ID: %s" % (
                        latest_request_id, history_obj.request_id
                    ))
                    if latest_request_id:
                        if history_obj.request_id != latest_request_id:
                            history_obj.request_id = latest_request_id
                            await history_obj.save()

            if PROCESSED and not validate_minute_withdrawal():
                PROCESSED = False

        except Exception as e:
            logger.exception("ERR_MONITOR_WITHDRAWAL: %s" % e)
        finally:
            await asyncio.sleep(1)


@dp.message(Command('help'))
async def help_command(message: Message) -> None:
    await message.reply(xtras.help_message, parse_mode=ParseMode.MARKDOWN)

@dp.message(Command('start'))
async def start_command(message: Message) -> None:
    print('here')
    chat_id = message.chat.id  # Retrieve chat ID  
    print("chat_id: ", chat_id)
    if BROADCAST_EVENT.is_set():
        await message.reply("Broadcast is already running!!")
    else:
        BROADCAST_EVENT.set()
        await message.reply("Broadcast has been started!!")
        await broadcast()

@dp.message(Command('stop'))
async def stop_command(message: Message) -> None:
    print('here')
    if BROADCAST_EVENT.is_set():
        BROADCAST_EVENT.clear()
        await message.reply("Broadcast has been stopped!!")
    else:
        await message.reply("Broadcast is not running at the moment!!")

@dp.message(Command('check_withdrawal'))
async def check_withdrawal_command(message: Message) -> None:
    
    stats = await process_statistics(
        period="Current week",
        update_db=False
    )

    if balance := validate_amount(stats.get("balance_current")):
        await message.reply("Balance is available for withdrawal: $%s" % balance)
    elif bonus := validate_amount(stats.get("bonus_current")):
        await message.reply("Bonus is available for withdrawal: $%s" % bonus)
    else:
        await message.reply("Withdrawal is not possible at the moment!!")

@dp.message(Command('autowithdrawal'))
async def autowithdrawal_command(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.reply("Please specify 'on' or 'off'")
        return
        
    action = command.args.lower()
    if action not in ['on', 'off']:
        await message.reply("Invalid argument. Use 'on' or 'off'")
        return

    if action == "on":
        if await models.is_auto_withdrawal_active():
            await message.reply(text=core.fix_message_format("It is already active!!"), parse_mode=ParseMode.MARKDOWN)
        else:
            await models.toggle_auto_withdrawal(action)
            await message.reply(text=core.fix_message_format("It has been enabled!!"), parse_mode=ParseMode.MARKDOWN)
            logger.debug("Auto-withdrawal has been turned on!!")
    else:
        if await models.is_auto_withdrawal_active():
            await models.toggle_auto_withdrawal(action)
            await message.reply(text=core.fix_message_format("It has been disabled!!"), parse_mode=ParseMode.MARKDOWN)
            logger.debug("Auto-withdrawal has been turned off!!")
        else:
            await message.reply(text=core.fix_message_format("It is already inactive!!"), parse_mode=ParseMode.MARKDOWN)

if __name__ == "__main__":
    async def run_background_task(coro, name):
        try:
            await coro
        except asyncio.CancelledError:
            logger.info(f"{name} task was cancelled")
        except Exception as e:
            logger.exception(f"Error in {name} task: {e}")

    async def main():
        try:
            await db_init()
            logger.debug("======== New Session ========")

            transport = httpx.AsyncHTTPTransport(retries=3)
            core.session = httpx.AsyncClient(
                headers=core.base_headers,
                transport=transport,
                follow_redirects=True
            )

            await perform_login()
            
            broadcast_task = asyncio.create_task(run_background_task(broadcast(), "broadcast"))
            monitor_task = asyncio.create_task(run_background_task(monitor_withdrawal(), "monitor"))
            
            await dp.start_polling(bot)
            
            broadcast_task.cancel()
            monitor_task.cancel()
            
            # Wait for tasks to finish
            await asyncio.gather(broadcast_task, monitor_task, return_exceptions=True)
            
        finally:
            # Cleanup resources
            await core.session.aclose()
            await db_close()

    # Use asyncio.run() which properly handles cleanup
    asyncio.run(main())