# -*- coding: utf-8 -*-
"""Contrato central de data/hora do aplicativo.

Regras permanentes desta camada:
- timestamps operacionais persistidos são timezone-aware e normalizados em UTC;
- horários de calendário/agendamento usam timezone IANA explícito;
- nenhuma decisão de scheduling depende do timezone atual do Windows;
- um agendamento preserva o horário local pretendido, o timezone IANA, o instante
  UTC correspondente e a origem da escolha do horário;
- datetimes legados sem offset só são interpretados quando há um timezone IANA
  explícito da conta para fornecer contexto.
"""
from __future__ import annotations

from datetime import date, datetime, time as dt_time, timezone
from functools import lru_cache
from typing import Mapping, MutableMapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc

SCHEDULE_TIME_MANUAL = "MANUAL"
SCHEDULE_TIME_RECOMMENDED = "RECOMMENDED"
SCHEDULE_TIME_ORIGINS = frozenset({SCHEDULE_TIME_MANUAL, SCHEDULE_TIME_RECOMMENDED})

# Os presets atuais já são mercados explícitos. Mercados com vários fusos usam
# um default determinístico; a conta persiste esse IANA e pode ser alterada
# explicitamente depois. Nunca inferimos pelo timezone atual do Windows.
DEFAULT_TIMEZONE_BY_COUNTRY = {
    "estados unidos": "America/New_York",
    "brasil": "America/Sao_Paulo",
    "méxico": "America/Mexico_City",
    "mexico": "America/Mexico_City",
    "espanha": "Europe/Madrid",
    "reino unido": "Europe/London",
    "canadá": "America/Toronto",
    "canada": "America/Toronto",
    "alemanha": "Europe/Berlin",
    "frança": "Europe/Paris",
    "franca": "Europe/Paris",
    "itália": "Europe/Rome",
    "italia": "Europe/Rome",
    "japão": "Asia/Tokyo",
    "japao": "Asia/Tokyo",
    "coreia do sul": "Asia/Seoul",
    "índia": "Asia/Kolkata",
    "india": "Asia/Kolkata",
    "indonésia": "Asia/Jakarta",
    "indonesia": "Asia/Jakarta",
}


class InvalidTimezoneError(ValueError):
    """Timezone não reconhecido pela base IANA."""


class MissingTimezoneConfigurationError(InvalidTimezoneError):
    """A conta/agendamento não possui timezone explícito nem default seguro."""


class NonexistentLocalTimeError(ValueError):
    """Horário de parede inexistente por salto de horário de verão."""


class InconsistentScheduleTimeError(ValueError):
    """Horário local/timezone não representa o mesmo instante UTC informado."""


def _country_key(value: object) -> str:
    return str(value or "").strip().casefold()


@lru_cache(maxsize=128)
def iana_zone(timezone_name: str) -> ZoneInfo:
    name = str(timezone_name or "").strip()
    if not name:
        raise InvalidTimezoneError("timezone IANA não pode ser vazio")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise InvalidTimezoneError(f"timezone IANA inválido: {name}") from exc


def validate_timezone_name(timezone_name: str) -> str:
    name = str(timezone_name or "").strip()
    iana_zone(name)
    return name


def validate_schedule_time_origin(value: str) -> str:
    origin = str(value or "").strip().upper()
    if origin not in SCHEDULE_TIME_ORIGINS:
        raise ValueError(
            "time_origin deve ser MANUAL ou RECOMMENDED"
        )
    return origin


def default_timezone_for_country(country: object) -> str:
    """Retorna somente defaults determinísticos de mercados oficialmente mapeados.

    Ausência de informação nunca vira UTC silenciosamente. UTC continua sendo um
    timezone IANA válido, mas deve ser configurado explicitamente na conta.
    """
    key = _country_key(country)
    timezone_name = DEFAULT_TIMEZONE_BY_COUNTRY.get(key)
    if not timezone_name:
        label = str(country or "").strip() or "(vazio)"
        raise MissingTimezoneConfigurationError(
            f"timezone_iana obrigatório: não há default seguro para o mercado {label!r}"
        )
    return validate_timezone_name(timezone_name)


def timezone_name_from_config(config: Mapping[str, object]) -> str:
    """Resolve timezone sem consultar ou inferir pelo timezone atual do SO.

    Precedência:
    1. timezone IANA explicitamente configurado;
    2. default determinístico de mercado oficial mapeado;
    3. erro explícito quando não há informação suficiente.
    """
    explicit = (
        config.get("timezone_iana")
        or config.get("timezone_name")  # alias legado/compatível
        or config.get("timezone")
    )
    if explicit:
        return validate_timezone_name(str(explicit))
    return default_timezone_for_country(config.get("pais_alvo"))


def ensure_timezone_config(config: MutableMapping[str, object]) -> tuple[str, bool]:
    """Garante ``timezone_iana`` explícito; retorna ``(nome, alterou)``."""
    name = timezone_name_from_config(config)
    changed = config.get("timezone_iana") != name
    if changed:
        config["timezone_iana"] = name
    return name, changed


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime interno deve possuir timezone explícito")
    return value.astimezone(UTC)


def utc_now_iso(*, timespec: str = "seconds") -> str:
    return utc_now().isoformat(timespec=timespec)


def utc_iso(value: datetime, *, timespec: str = "seconds") -> str:
    return to_utc(value).isoformat(timespec=timespec)


def _parse_iso(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp deve ser texto ISO-8601 não vazio")
    raw = value.strip()
    if raw.endswith("Z") or raw.endswith("z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"timestamp ISO-8601 inválido: {value}") from exc


def normalize_utc_iso(value: str, *, timespec: str = "seconds") -> str:
    """Normaliza timestamp timezone-aware para representação UTC."""
    parsed = _parse_iso(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp persistido deve possuir offset/timezone explícito")
    return parsed.astimezone(UTC).isoformat(timespec=timespec)


def parse_utc_iso(value: str) -> datetime:
    parsed = _parse_iso(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp UTC persistido não pode ser naive")
    return parsed.astimezone(UTC)


def parse_local_iso(value: str) -> datetime:
    """Lê um horário local pretendido, que por contrato não possui offset."""
    parsed = _parse_iso(value)
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        raise ValueError("scheduled_local deve ser um horário local sem offset")
    return parsed.replace(tzinfo=None)


def local_datetime_candidates_utc(local_value: datetime, timezone_name: str) -> tuple[datetime, ...]:
    """Retorna instantes UTC válidos para um horário de parede.

    Horários normais geram um candidato; horários ambíguos de fim de DST podem
    gerar dois; horários inexistentes geram zero candidatos.
    """
    zone = iana_zone(timezone_name)
    naive = local_value.replace(tzinfo=None)
    candidates: list[datetime] = []
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        as_utc = aware.astimezone(UTC)
        roundtrip = as_utc.astimezone(zone)
        if roundtrip.replace(tzinfo=None) == naive:
            if all(existing != as_utc for existing in candidates):
                candidates.append(as_utc)
    return tuple(sorted(candidates))


def local_datetime_to_utc(local_value: datetime, timezone_name: str) -> datetime:
    """Converte horário local para UTC, validando DST.

    Em horário ambíguo escolhe deterministicamente a primeira ocorrência
    cronológica. A representação canônica persiste também o UTC, então o
    instante fica estável depois da criação. Em horário inexistente, falha em
    vez de mover silenciosamente a intenção do usuário.
    """
    candidates = local_datetime_candidates_utc(local_value, timezone_name)
    if not candidates:
        naive = local_value.replace(tzinfo=None)
        raise NonexistentLocalTimeError(
            f"horário local inexistente em {timezone_name}: {naive.isoformat(timespec='minutes')}"
        )
    return candidates[0]


def local_wall_time_to_utc(day: date, hhmm: str, timezone_name: str) -> datetime:
    try:
        hour, minute = map(int, str(hhmm).split(":"))
        wall = datetime.combine(day, dt_time(hour=hour, minute=minute))
    except Exception as exc:
        raise ValueError(f"horário HH:MM inválido: {hhmm}") from exc
    return local_datetime_to_utc(wall, timezone_name)


def utc_to_local(value: datetime, timezone_name: str) -> datetime:
    return to_utc(value).astimezone(iana_zone(timezone_name))


def normalize_local_iso(value: str, *, timespec: str = "seconds") -> str:
    return parse_local_iso(value).isoformat(timespec=timespec)


def canonical_schedule_values(
    *,
    scheduled_local: str | None,
    timezone_iana: str,
    scheduled_utc: str | None,
) -> tuple[str | None, str, str | None]:
    """Valida/completa a representação canônica de um agendamento.

    Se apenas local ou UTC for fornecido, deriva o outro lado. Quando ambos são
    fornecidos, verifica que representam o mesmo instante (incluindo o caso de
    horário ambíguo no fim do DST).
    """
    timezone_iana = validate_timezone_name(timezone_iana)
    if scheduled_local is None and scheduled_utc is None:
        return None, timezone_iana, None

    local_dt = parse_local_iso(scheduled_local) if scheduled_local is not None else None
    utc_dt = parse_utc_iso(scheduled_utc) if scheduled_utc is not None else None

    if local_dt is None:
        assert utc_dt is not None
        local_dt = utc_to_local(utc_dt, timezone_iana).replace(tzinfo=None)
    elif utc_dt is None:
        utc_dt = local_datetime_to_utc(local_dt, timezone_iana)
    else:
        candidates = local_datetime_candidates_utc(local_dt, timezone_iana)
        if not candidates:
            raise NonexistentLocalTimeError(
                f"horário local inexistente em {timezone_iana}: "
                f"{local_dt.isoformat(timespec='minutes')}"
            )
        if utc_dt not in candidates:
            raise InconsistentScheduleTimeError(
                "scheduled_local + timezone_iana não correspondem a scheduled_utc"
            )

    return (
        local_dt.isoformat(timespec="seconds"),
        timezone_iana,
        utc_dt.astimezone(UTC).isoformat(timespec="seconds"),
    )


def canonical_schedule_fields(
    local_value: datetime,
    timezone_name: str,
    *,
    time_origin: str = SCHEDULE_TIME_MANUAL,
    include_legacy_datetime: bool = True,
) -> dict[str, str]:
    """Cria os campos persistíveis canônicos de um slot calculado.

    ``datetime`` é mantido apenas como alias legado de leitura humana/compatível;
    os campos canônicos são ``scheduled_local``, ``timezone_iana``,
    ``scheduled_utc`` e ``time_origin``.
    """
    timezone_name = validate_timezone_name(timezone_name)
    origin = validate_schedule_time_origin(time_origin)

    if local_value.tzinfo is not None and local_value.utcoffset() is not None:
        # Se o chamador entregou um datetime aware, primeiro o expressamos no
        # timezone canônico da conta; não confiamos no timezone do Windows.
        local_aware = local_value.astimezone(iana_zone(timezone_name))
        local_naive = local_aware.replace(tzinfo=None)
        instant_utc = local_aware.astimezone(UTC)
    else:
        local_naive = local_value.replace(tzinfo=None)
        instant_utc = local_datetime_to_utc(local_naive, timezone_name)

    scheduled_local = local_naive.isoformat(timespec="minutes")
    result = {
        "scheduled_local": scheduled_local,
        "timezone_iana": timezone_name,
        "scheduled_utc": instant_utc.isoformat(timespec="minutes"),
        "time_origin": origin,
    }
    if include_legacy_datetime:
        result["datetime"] = scheduled_local
    return result


def schedule_utc_from_record(record: Mapping[str, object], fallback_timezone: str) -> datetime | None:
    """Lê registro canônico ou legado e devolve seu instante UTC.

    Prioridade: ``scheduled_utc`` -> ``scheduled_local`` -> ``datetime`` legado.
    Registros canônicos preservam o timezone próprio mesmo se a conta depois
    mudar de timezone.
    """
    timezone_name = validate_timezone_name(
        str(record.get("timezone_iana") or fallback_timezone)
    )

    raw_utc = record.get("scheduled_utc")
    if raw_utc:
        try:
            return parse_utc_iso(str(raw_utc))
        except (TypeError, ValueError):
            return None

    raw_local = record.get("scheduled_local")
    if raw_local:
        try:
            return local_datetime_to_utc(parse_local_iso(str(raw_local)), timezone_name)
        except (TypeError, ValueError):
            return None

    legacy = record.get("datetime")
    if not legacy:
        return None
    try:
        parsed = _parse_iso(str(legacy))
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(UTC)
        return local_datetime_to_utc(parsed, timezone_name)
    except (TypeError, ValueError):
        return None


def schedule_local_from_record(record: Mapping[str, object], fallback_timezone: str) -> datetime | None:
    """Lê registro canônico/legado e retorna datetime aware no timezone do registro."""
    timezone_name = validate_timezone_name(
        str(record.get("timezone_iana") or fallback_timezone)
    )
    instant = schedule_utc_from_record(record, timezone_name)
    if instant is None:
        return None
    return utc_to_local(instant, timezone_name)


def local_today(timezone_name: str, *, now_utc: datetime | None = None) -> date:
    current = to_utc(now_utc) if now_utc is not None else utc_now()
    return current.astimezone(iana_zone(timezone_name)).date()


def format_local(
    value: datetime,
    timezone_name: str,
    fmt: str = "%d/%m/%Y %H:%M",
) -> str:
    return utc_to_local(value, timezone_name).strftime(fmt)
