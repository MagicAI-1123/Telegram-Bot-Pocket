from tortoise.models import Model
from tortoise import fields

from datetime import datetime, timedelta
import pytz

db_name = "pocketoption-local.db"
logger = None


def current_hour() -> int:
    return datetime.now(tz=pytz.utc).time().hour


class Statistics(Model):
    period = fields.CharField(max_length=50, unique=True, null=False)
    deposits = fields.FloatField(null=False)
    old_deposits = fields.FloatField(null=False)
    commission = fields.FloatField(null=False)
    old_commission = fields.FloatField(null=False)
    withdrawals = fields.FloatField(null=False)
    old_withdrawals = fields.FloatField(null=False)
    hold = fields.FloatField(null=False)
    old_hold = fields.FloatField(null=False)
    pool = fields.FloatField(null=False)
    old_pool = fields.FloatField(null=False)
    balance = fields.FloatField(null=False)
    old_balance = fields.FloatField(null=False)
    bonus = fields.FloatField(null=False)
    old_bonus = fields.FloatField(null=False)
    account_status = fields.CharField(max_length=50, null=True)
    updated = fields.DatetimeField(auto_now_add=True)

    def __str__(self):
        return "Statistics: %s" % self.period


class StatisticsLog(Model):
    period = fields.CharField(max_length=50, null=False)
    deposits = fields.FloatField(null=False)
    commission = fields.FloatField(null=False)
    withdrawals = fields.FloatField(null=False)
    hold = fields.FloatField(null=False)
    pool = fields.FloatField(null=False)
    balance = fields.FloatField(null=False)
    bonus = fields.FloatField(null=False)
    visitors = fields.FloatField(null=False)
    registrations = fields.FloatField(null=False)
    registrations_avg = fields.FloatField(null=False)
    ftd = fields.FloatField(null=False)
    ftd_avg = fields.FloatField(null=False)
    account_status = fields.CharField(max_length=50, null=True)
    run_hour = fields.IntField(null=False, default=current_hour)
    updated = fields.DatetimeField(auto_now_add=True)

    def __str__(self):
        return "StatisticsLog: %s | %s" % (self.period, self.run_hour)


class History(Model):
    request_id = fields.CharField(max_length=50, null=True)
    updated = fields.DatetimeField(auto_now_add=True)

    def __str__(self):
        return "History: %s" % self.request_id


class Withdrawal(Model):
    id = fields.IntField(pk=True)

    auto = fields.BooleanField(default=False)
    auto_all = fields.BooleanField(default=True)

    updated = fields.DatetimeField(auto_now_add=True)

    def __str__(self) -> str:
        return "Withdrawal -> Auto: %s (All amount: %s)" % (
            self.auto, self.auto_all
        )


async def is_auto_withdrawal_active() -> bool:
    try:
        obj = await Withdrawal.first()
    except Exception as e:
        logger.exception("ERR_IS_AUTO_WITHDRAWAL_ACIVE: %s" % e)
    else:
        return obj.auto


async def toggle_auto_withdrawal(toggle: str) -> None:
    try:
        obj = await Withdrawal.first()
        obj.auto = toggle.lower().strip() == "on"
        await obj.save()
    except Exception as e:
        logger.exception("ERR_TOGGLE_AUTO_WITHDRAWAL: %s" % e)



def query_str(date: datetime.date, hour: int) -> str:
    return "%s %s" % (
        date, f"{hour:0>2}"
    )


async def get_last_log() -> StatisticsLog:
    try:
        obj = await StatisticsLog.filter().order_by("-id").first()
    except Exception as e:
        logger.exception("ERR_GET_LAST_LOG: %s" % e)
    else:
        return obj
    finally:
        logger.debug("Date: %s | Hour: %s" % (
            obj.updated.date(), obj.updated.time().hour
        ))


async def get_log_data(date: datetime.date = None, hour: int = None, failsafe: bool = False) -> StatisticsLog:
    if date is None:
        date = datetime.now(tz=pytz.utc).date() - timedelta(days=7)
    elif isinstance(date, str):
        date = datetime.strptime("%Y-%m-%s", date)

    if hour is None:
        hour = current_hour()

    logger.debug("Date: %s | Hour: %s" % (date, hour))
    required_date = date
    one_less_required_hour = hour - 1

    if one_less_required_hour < 0:
        one_less_required_hour = 23
        required_date = date - timedelta(days=1)

    try:
        return await StatisticsLog.filter(
            updated__startswith=query_str(date, hour),
        ).first() or await StatisticsLog.filter(
            updated__startswith=query_str(
                required_date, one_less_required_hour),
        ).first()
    except Exception as e:
        logger.exception("ERR_GET_LOG_DATA: %s | %s" % (
            date, hour
        ))


# if __name__ == "__main__":
#     import asyncio

#     loop = asyncio.new_event_loop()
#     x = loop.run_until_complete(get_log_data())
#     logger.debug(x)
