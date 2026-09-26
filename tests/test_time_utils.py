from datetime import datetime, timezone

import pytest

from _sistema import time_utils


def test_timezone_iana_padrao_por_mercado_e_persistencia_em_config():
    cfg = {"pais_alvo": "Brasil"}
    name, changed = time_utils.ensure_timezone_config(cfg)

    assert name == "America/Sao_Paulo"
    assert changed is True
    assert cfg["timezone_iana"] == "America/Sao_Paulo"

    name2, changed2 = time_utils.ensure_timezone_config(cfg)
    assert name2 == name
    assert changed2 is False


def test_timezone_invalido_e_rejeitado_sem_usar_timezone_do_windows():
    with pytest.raises(time_utils.InvalidTimezoneError):
        time_utils.validate_timezone_name("GMT-03:00")


def test_local_today_depende_do_timezone_iana_e_nao_do_timezone_da_maquina():
    instant = datetime(2026, 9, 17, 2, 0, tzinfo=timezone.utc)

    assert time_utils.local_today("America/Sao_Paulo", now_utc=instant).isoformat() == "2026-09-16"
    assert time_utils.local_today("Asia/Tokyo", now_utc=instant).isoformat() == "2026-09-17"


def test_dst_horario_inexistente_nao_e_movido_silenciosamente():
    # Nova York pula de 01:59 para 03:00 em 2026-03-08.
    local = datetime(2026, 3, 8, 2, 30)
    with pytest.raises(time_utils.NonexistentLocalTimeError):
        time_utils.local_datetime_to_utc(local, "America/New_York")


def test_dst_horario_ambiguo_tem_instante_deterministico_e_pode_preservar_segunda_ocorrencia():
    local = datetime(2026, 11, 1, 1, 30)
    candidates = time_utils.local_datetime_candidates_utc(local, "America/New_York")

    assert candidates == (
        datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc),
        datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc),
    )
    assert time_utils.local_datetime_to_utc(local, "America/New_York") == candidates[0]

    local_iso, tz_name, utc_iso = time_utils.canonical_schedule_values(
        scheduled_local="2026-11-01T01:30",
        timezone_iana="America/New_York",
        scheduled_utc="2026-11-01T06:30:00+00:00",
    )
    assert local_iso == "2026-11-01T01:30:00"
    assert tz_name == "America/New_York"
    assert utc_iso == "2026-11-01T06:30:00+00:00"


def test_canonical_schedule_fields_preserva_intencao_local_timezone_utc_e_origem():
    fields = time_utils.canonical_schedule_fields(
        datetime(2026, 9, 17, 10, 0),
        "America/Sao_Paulo",
        time_origin=time_utils.SCHEDULE_TIME_MANUAL,
    )

    assert fields == {
        "scheduled_local": "2026-09-17T10:00",
        "timezone_iana": "America/Sao_Paulo",
        "scheduled_utc": "2026-09-17T13:00+00:00",
        "time_origin": "MANUAL",
        "datetime": "2026-09-17T10:00",
    }


def test_registro_preserva_timezone_original_mesmo_se_conta_mudar_de_timezone():
    record = {
        "scheduled_local": "2026-07-01T10:00",
        "timezone_iana": "America/New_York",
        "scheduled_utc": "2026-07-01T14:00+00:00",
        "time_origin": "MANUAL",
    }

    instant = time_utils.schedule_utc_from_record(record, "Asia/Tokyo")
    assert instant == datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)
    assert time_utils.utc_to_local(instant, "Asia/Tokyo").strftime("%Y-%m-%d %H:%M") == "2026-07-01 23:00"


def test_historico_legado_naive_e_interpretado_no_timezone_explicito_da_conta():
    instant = time_utils.schedule_utc_from_record(
        {"datetime": "2026-09-17T10:00"},
        "America/Sao_Paulo",
    )
    assert instant == datetime(2026, 9, 17, 13, 0, tzinfo=timezone.utc)


def test_timestamp_operacional_utc_possui_offset_explicito():
    value = time_utils.utc_now_iso()
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def test_brasil_continua_com_default_oficial_sao_paulo():
    assert time_utils.default_timezone_for_country("Brasil") == "America/Sao_Paulo"


def test_estados_unidos_continua_com_default_oficial_new_york():
    assert time_utils.default_timezone_for_country("Estados Unidos") == "America/New_York"


def test_todos_os_presets_oficiais_possuem_timezone_iana_valido():
    from _sistema import painel_oficial as painel

    for country, _locale, _label in painel.PRESETS.values():
        timezone_name = time_utils.default_timezone_for_country(country)
        assert time_utils.validate_timezone_name(timezone_name) == timezone_name


@pytest.mark.parametrize("country", ["Personalizado", "Portugal", "Argentina", "", None])
def test_mercado_sem_default_seguro_nao_vira_utc(country):
    with pytest.raises(time_utils.MissingTimezoneConfigurationError, match="timezone_iana"):
        time_utils.timezone_name_from_config({"pais_alvo": country})


def test_utc_explicitamente_configurado_continua_valido_para_mercado_desconhecido():
    cfg = {"pais_alvo": "Personalizado", "timezone_iana": "UTC"}
    assert time_utils.timezone_name_from_config(cfg) == "UTC"


def test_timezone_explicito_sempre_vence_default_do_pais():
    cfg = {"pais_alvo": "Brasil", "timezone_iana": "Asia/Tokyo"}
    assert time_utils.timezone_name_from_config(cfg) == "Asia/Tokyo"


def test_resolucao_de_timezone_nao_depende_do_timezone_do_sistema(monkeypatch):
    cfg = {"pais_alvo": "Brasil"}
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    first = time_utils.timezone_name_from_config(cfg)
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    second = time_utils.timezone_name_from_config(cfg)
    assert first == second == "America/Sao_Paulo"


def test_ensure_timezone_config_em_falha_nao_altera_config():
    cfg = {"pais_alvo": "Personalizado", "nome_conta": "Canal"}
    before = dict(cfg)
    with pytest.raises(time_utils.MissingTimezoneConfigurationError):
        time_utils.ensure_timezone_config(cfg)
    assert cfg == before


def test_timezone_explicito_invalido_nao_cai_para_default_do_pais():
    cfg = {"pais_alvo": "Brasil", "timezone_iana": "timezone-invalido"}
    with pytest.raises(time_utils.InvalidTimezoneError):
        time_utils.timezone_name_from_config(cfg)
