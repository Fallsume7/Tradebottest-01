"""SQLite persistence for the paper broker + app records, with restart recovery."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, delete, event, select
from sqlalchemy.orm import Session, sessionmaker

from mwbot.config import AppSettings
from mwbot.db.models import (
    AccountRow,
    BacktestRow,
    Base,
    DailyReportRow,
    DecisionRow,
    EligibilityRow,
    EquityRow,
    FillRow,
    KeyValueRow,
    OrderRow,
    PositionRow,
    TradeRow,
)
from mwbot.domain import (
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Side,
    TimeInForce,
    Trade,
)
from mwbot.eligibility.models import EligibilityRecord, EligibilityStatus
from mwbot.eligibility.universe import EligibilityUniverse
from mwbot.trading.broker import PaperBroker

log = logging.getLogger(__name__)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite:///") and ":memory:" not in url:
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    eng = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn: Any, _rec: Any) -> None:  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    Base.metadata.create_all(eng)
    return eng


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _position_to_json(p: Position) -> dict[str, Any]:
    d = asdict(p)
    d["opened_at"] = p.opened_at.isoformat()
    d["deadline"] = p.deadline.isoformat() if p.deadline else None
    return json.loads(json.dumps(d, default=str))


def _position_from_json(d: dict[str, Any]) -> Position:
    d = dict(d)
    d["opened_at"] = datetime.fromisoformat(d["opened_at"])
    d["deadline"] = datetime.fromisoformat(d["deadline"]) if d.get("deadline") else None
    known = {f.name for f in fields(Position)}
    return Position(**{k: v for k, v in d.items() if k in known})


def _trade_to_json(t: Trade) -> dict[str, Any]:
    d = asdict(t)
    d["entry_time"] = t.entry_time.isoformat()
    d["exit_time"] = t.exit_time.isoformat()
    return d


def _trade_from_json(d: dict[str, Any]) -> Trade:
    d = dict(d)
    d["entry_time"] = datetime.fromisoformat(d["entry_time"])
    d["exit_time"] = datetime.fromisoformat(d["exit_time"])
    known = {f.name for f in fields(Trade)}
    return Trade(**{k: v for k, v in d.items() if k in known})


class SqlStore:
    """Implements ``BrokerStore``; also stores settings, reports, equity, decisions, backtests."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._sessions = sessionmaker(engine, expire_on_commit=False)
        self._order_sig: dict[str, tuple[Any, ...]] = {}
        self._fills_saved = 0
        self._trades_saved: set[str] = set()

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._sessions()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # ------------------------------------------------------------------ key/value (settings)
    def get_kv(self, key: str, default: Any = None) -> Any:
        with self.session() as s:
            row = s.get(KeyValueRow, key)
            return row.value if row else default

    def set_kv(self, key: str, value: Any) -> None:
        with self.session() as s:
            row = s.get(KeyValueRow, key)
            if row:
                row.value = value
            else:
                s.add(KeyValueRow(key=key, value=value))

    def load_settings(self) -> AppSettings:
        raw = self.get_kv("settings")
        return AppSettings.model_validate(raw) if raw else AppSettings()

    def save_settings(self, settings: AppSettings) -> None:
        self.set_kv("settings", json.loads(settings.model_dump_json()))

    # ------------------------------------------------------------------ broker persistence
    def persist(self, broker: PaperBroker) -> None:
        with self.session() as s:
            now = datetime.now(UTC)
            acct = s.get(AccountRow, 1)
            vals = dict(
                cash=broker.cash,
                starting_cash=broker.starting_cash,
                realized_pnl=broker.realized_pnl,
                commissions_paid=broker.commissions_paid,
                order_counter=broker._counter,
                trade_counter=broker._trade_counter,
                session_date=broker.session_date,
                marks=broker.marks,
                updated_at=now,
            )
            if acct is None:
                s.add(AccountRow(id=1, **vals))
            else:
                for k, v in vals.items():
                    setattr(acct, k, v)
            s.execute(delete(PositionRow))
            for p in broker.positions.values():
                s.add(PositionRow(symbol=p.symbol, data=_position_to_json(p)))
            for o in broker.orders.values():
                sig = (o.status, o.filled_quantity, o.updated_at)
                if self._order_sig.get(o.id) == sig:
                    continue
                self._order_sig[o.id] = sig
                row = s.get(OrderRow, o.id)
                data = dict(
                    client_order_id=o.client_order_id,
                    symbol=o.symbol,
                    side=str(o.side),
                    quantity=o.quantity,
                    order_type=str(o.order_type),
                    limit_price=o.limit_price,
                    time_in_force=str(o.time_in_force),
                    status=str(o.status),
                    filled_quantity=o.filled_quantity,
                    avg_fill_price=o.avg_fill_price,
                    commission=o.commission,
                    strategy=o.strategy,
                    reason=o.reason,
                    reject_reason=o.reject_reason,
                    created_at=o.created_at,
                    updated_at=o.updated_at,
                    eligible_at=o.eligible_at,
                    expires_at=o.expires_at,
                    meta=json.loads(json.dumps(o.meta, default=str)),
                    history=[[t.isoformat(), a, b] for t, a, b in o.history],
                )
                if row is None:
                    s.add(OrderRow(id=o.id, **data))
                else:
                    for k, v in data.items():
                        setattr(row, k, v)
            for f in broker.fills[self._fills_saved :]:
                s.add(
                    FillRow(
                        order_id=f.order_id,
                        symbol=f.symbol,
                        side=str(f.side),
                        quantity=f.quantity,
                        price=f.price,
                        commission=f.commission,
                        timestamp=f.timestamp,
                        note=f.note,
                    )
                )
            self._fills_saved = len(broker.fills)
            for t in broker.trades:
                if t.id in self._trades_saved:
                    continue
                s.add(
                    TradeRow(
                        id=t.id,
                        symbol=t.symbol,
                        strategy=t.strategy,
                        session_date=t.session_date,
                        exit_time=t.exit_time,
                        data=_trade_to_json(t),
                    )
                )
                self._trades_saved.add(t.id)

    def restore_broker(self, broker: PaperBroker) -> bool:
        """Load persisted state into ``broker``. Returns True if an account existed."""
        with self.session() as s:
            acct = s.get(AccountRow, 1)
            if acct is None:
                return False
            broker.cash = acct.cash
            broker.starting_cash = acct.starting_cash
            broker.realized_pnl = acct.realized_pnl
            broker.commissions_paid = acct.commissions_paid
            broker._counter = acct.order_counter
            broker._trade_counter = acct.trade_counter
            broker.session_date = acct.session_date
            broker.marks = dict(acct.marks or {})
            broker.positions = {r.symbol: _position_from_json(r.data) for r in s.scalars(select(PositionRow))}
            broker.orders.clear()
            broker._by_client.clear()
            for r in s.scalars(select(OrderRow).order_by(OrderRow.id)):
                o = Order(
                    id=r.id,
                    client_order_id=r.client_order_id,
                    symbol=r.symbol,
                    side=Side(r.side),
                    quantity=r.quantity,
                    order_type=OrderType(r.order_type),
                    limit_price=r.limit_price,
                    time_in_force=TimeInForce(r.time_in_force),
                    strategy=r.strategy,
                    reason=r.reason,
                    created_at=r.created_at,
                    status=OrderStatus(r.status),
                    filled_quantity=r.filled_quantity,
                    avg_fill_price=r.avg_fill_price,
                    commission=r.commission,
                    updated_at=r.updated_at,
                    reject_reason=r.reject_reason,
                    eligible_at=r.eligible_at,
                    expires_at=r.expires_at,
                    meta=dict(r.meta or {}),
                    history=[(datetime.fromisoformat(t), a, b) for t, a, b in (r.history or [])],
                )
                broker.orders[o.id] = o
                broker._by_client[o.client_order_id] = o.id
                self._order_sig[o.id] = (o.status, o.filled_quantity, o.updated_at)
                if not o.is_terminal and o.eligible_at:
                    # resume scanning from the eligibility minute; already-applied fills are not repeated
                    # because remaining quantity is persisted.
                    from mwbot.trading.broker import _ceil_minute

                    broker._cursor[o.id] = _ceil_minute(o.eligible_at)
            broker.fills = [
                Fill(
                    r.order_id, r.symbol, Side(r.side), r.quantity, r.price, r.commission, r.timestamp, r.note
                )
                for r in s.scalars(select(FillRow).order_by(FillRow.id))
            ]
            self._fills_saved = len(broker.fills)
            broker.trades = [
                _trade_from_json(r.data)
                for r in s.scalars(select(TradeRow).order_by(TradeRow.exit_time, TradeRow.id))
            ]
            self._trades_saved = {t.id for t in broker.trades}
        log.info("restored paper account", extra={"cash": broker.cash, "positions": len(broker.positions)})
        return True

    def reset_account(self) -> None:
        with self.session() as s:
            for model in (
                AccountRow,
                PositionRow,
                OrderRow,
                FillRow,
                TradeRow,
                EquityRow,
                DecisionRow,
                DailyReportRow,
            ):
                s.execute(delete(model))
        self._order_sig.clear()
        self._fills_saved = 0
        self._trades_saved.clear()

    # ------------------------------------------------------------------ equity / decisions / reports
    def add_equity(self, ts: datetime, equity: float, session_date: str) -> None:
        with self.session() as s:
            s.add(EquityRow(ts=ts, equity=equity, session_date=session_date))

    def equity_history(self, limit: int = 5000) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.scalars(select(EquityRow).order_by(EquityRow.id.desc()).limit(limit)).all()
        return [{"ts": r.ts.isoformat(), "equity": r.equity, "date": r.session_date} for r in reversed(rows)]

    def add_decision(self, entry: dict[str, Any]) -> None:
        with self.session() as s:
            s.add(
                DecisionRow(
                    ts=entry["ts"],
                    kind=entry["kind"],
                    symbol=entry.get("symbol", ""),
                    strategy=entry.get("strategy", ""),
                    message=entry["message"],
                    details=json.loads(json.dumps(entry.get("details", {}), default=str)),
                )
            )

    def recent_decisions(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.scalars(select(DecisionRow).order_by(DecisionRow.id.desc()).limit(limit)).all()
        return [
            {
                "ts": r.ts,
                "kind": r.kind,
                "symbol": r.symbol,
                "strategy": r.strategy,
                "message": r.message,
                "details": r.details,
            }
            for r in rows
        ]

    def save_report(self, report: dict[str, Any]) -> None:
        with self.session() as s:
            row = s.get(DailyReportRow, report["date"])
            payload = json.loads(json.dumps(report, default=str))
            if row:
                row.data = payload
            else:
                s.add(DailyReportRow(date=report["date"], data=payload))

    def reports(self) -> list[dict[str, Any]]:
        with self.session() as s:
            return [r.data for r in s.scalars(select(DailyReportRow).order_by(DailyReportRow.date))]

    # ------------------------------------------------------------------ backtests
    def save_backtest(
        self, bt_id: str, name: str, config: dict[str, Any], summary: dict[str, Any], result: dict[str, Any]
    ) -> None:
        with self.session() as s:
            s.merge(
                BacktestRow(
                    id=bt_id,
                    created_at=datetime.now(UTC),
                    name=name,
                    config=json.loads(json.dumps(config, default=str)),
                    summary=json.loads(json.dumps(summary, default=str)),
                    result=json.loads(json.dumps(result, default=str)),
                )
            )

    def list_backtests(self) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.scalars(select(BacktestRow).order_by(BacktestRow.created_at.desc())).all()
        return [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat(),
                "name": r.name,
                "config": r.config,
                "summary": r.summary,
            }
            for r in rows
        ]

    def get_backtest(self, bt_id: str) -> dict[str, Any] | None:
        with self.session() as s:
            r = s.get(BacktestRow, bt_id)
            if r is None:
                return None
            return {
                "id": r.id,
                "created_at": r.created_at.isoformat(),
                "name": r.name,
                "config": r.config,
                "summary": r.summary,
                "result": r.result,
            }

    # ------------------------------------------------------------------ eligibility history
    def eligibility_upsert(self, records: list[EligibilityRecord]) -> int:
        n = 0
        with self.session() as s:
            for rec in records:
                row = s.scalars(
                    select(EligibilityRow).where(
                        EligibilityRow.ticker == rec.ticker.upper(),
                        EligibilityRow.effective_from == rec.effective_from.isoformat(),
                    )
                ).first()
                vals = dict(
                    status=str(rec.status),
                    verified_on=rec.verified_on.isoformat() if rec.verified_on else None,
                    name=rec.name,
                    exchange=rec.exchange,
                    security_type=rec.security_type,
                    rejection_reason=rec.rejection_reason,
                    restrictions=rec.restrictions,
                    source=rec.source,
                )
                if row is None:
                    s.add(
                        EligibilityRow(
                            ticker=rec.ticker.upper(), effective_from=rec.effective_from.isoformat(), **vals
                        )
                    )
                else:
                    for k, v in vals.items():
                        setattr(row, k, v)
                n += 1
        return n

    def eligibility_all(self) -> list[EligibilityRecord]:
        with self.session() as s:
            rows = s.scalars(
                select(EligibilityRow).order_by(EligibilityRow.ticker, EligibilityRow.effective_from)
            )
            return [_rec_from_row(r) for r in rows]

    def eligibility_delete(self, ticker: str) -> int:
        with self.session() as s:
            res = s.execute(delete(EligibilityRow).where(EligibilityRow.ticker == ticker.upper()))
            return int(res.rowcount or 0)  # type: ignore[attr-defined]

    def eligibility_clear(self) -> None:
        with self.session() as s:
            s.execute(delete(EligibilityRow))

    def eligibility_universe(self) -> EligibilityUniverse:
        return EligibilityUniverse(self.eligibility_all())


def _rec_from_row(r: EligibilityRow) -> EligibilityRecord:
    return EligibilityRecord(
        ticker=r.ticker,
        status=EligibilityStatus(r.status),
        effective_from=date.fromisoformat(r.effective_from),
        verified_on=date.fromisoformat(r.verified_on) if r.verified_on else None,
        name=r.name,
        exchange=r.exchange,
        security_type=r.security_type,
        rejection_reason=r.rejection_reason,
        restrictions=r.restrictions,
        source=r.source,
    )
