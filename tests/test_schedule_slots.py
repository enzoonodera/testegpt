from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import unittest

from _sistema import agendar_tiktok as tiktok
from _sistema import agendar_youtube as youtube


FIXED_NOW_UTC = datetime(2026, 9, 16, 13, 0, tzinfo=timezone.utc)  # 10:00 em São Paulo
SAO_PAULO = ZoneInfo("America/Sao_Paulo")


def wall_values(slots):
    return [(s.year, s.month, s.day, s.hour, s.minute) for s in slots]


class YoutubeSlotTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "timezone_iana": "America/Sao_Paulo",
            "horarios": ["19:00", "20:00", "21:00"],
            "horarios_por_dia": {
                "quinta": ["20:00", "12:00", "17:00"],
                "sexta": ["12:00", "17:00", "20:00"],
            },
            "max_anos_agendamento": 1,
            "comecar_amanha_na_primeira_execucao": True,
        }

    def test_primeira_execucao_comeca_amanha_e_ordena_horarios_do_dia(self):
        slots = youtube.build_slots(self.cfg, {"scheduled": []}, 4, now_utc=FIXED_NOW_UTC)
        self.assertEqual(
            [
                (2026, 9, 17, 12, 0),
                (2026, 9, 17, 17, 0),
                (2026, 9, 17, 20, 0),
                (2026, 9, 18, 12, 0),
            ],
            wall_values(slots),
        )
        self.assertTrue(all(s.tzinfo == SAO_PAULO for s in slots))

    def test_continua_apos_ultimo_slot_persistido_legado(self):
        state = {"scheduled": [{"datetime": "2026-09-17T17:00"}]}
        slots = youtube.build_slots(self.cfg, state, 2, now_utc=FIXED_NOW_UTC)
        self.assertEqual(
            [(2026, 9, 17, 20, 0), (2026, 9, 18, 12, 0)],
            wall_values(slots),
        )

    def test_continua_apos_historico_canonico_com_timezone_proprio(self):
        state = {"scheduled": [{
            "scheduled_local": "2026-09-17T17:00",
            "timezone_iana": "America/Sao_Paulo",
            "scheduled_utc": "2026-09-17T20:00+00:00",
            "time_origin": "MANUAL",
        }]}
        slots = youtube.build_slots(self.cfg, state, 1, now_utc=FIXED_NOW_UTC)
        self.assertEqual([(2026, 9, 17, 20, 0)], wall_values(slots))

    def test_parse_dt_invalido_retorna_none_e_valido_retorna_utc_aware(self):
        self.assertIsNone(youtube.parse_dt("nao-e-data", "America/Sao_Paulo"))
        parsed = youtube.parse_dt("2026-09-17T10:00", "America/Sao_Paulo")
        self.assertEqual(datetime(2026, 9, 17, 13, 0, tzinfo=timezone.utc), parsed)


class TiktokSlotTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "timezone_iana": "America/Sao_Paulo",
            "horarios": ["10:00", "15:00", "20:00"],
            "dias_janela": 9,
            "comecar_amanha_na_primeira_execucao": True,
        }

    def test_primeira_execucao_gera_slots_a_partir_de_amanha(self):
        slots = tiktok.build_slots(self.cfg, {"scheduled": []}, 4, now_utc=FIXED_NOW_UTC)
        self.assertEqual(4, len(slots))
        self.assertEqual(
            [
                (2026, 9, 17, 10, 0),
                (2026, 9, 17, 15, 0),
                (2026, 9, 17, 20, 0),
                (2026, 9, 18, 10, 0),
            ],
            wall_values(slots),
        )
        self.assertTrue(all(s.tzinfo == SAO_PAULO for s in slots))

    def test_tiktok_build_slots_respects_requested_quantity(self):
        requested_quantity = 1
        slots = tiktok.build_slots(
            self.cfg,
            {"scheduled": []},
            requested_quantity,
            now_utc=FIXED_NOW_UTC,
        )
        self.assertEqual(
            requested_quantity,
            len(slots),
            "build_slots não deve retornar mais slots do que a quantidade solicitada",
        )

    def test_continua_apos_maior_datetime_do_historico_legado(self):
        state = {"scheduled": [
            {"datetime": "2026-09-17T10:00"},
            {"datetime": "2026-09-17T15:00"},
            {"datetime": "invalido"},
        ]}
        slots = tiktok.build_slots(self.cfg, state, 3, now_utc=FIXED_NOW_UTC)
        self.assertEqual(
            [(2026, 9, 17, 20, 0), (2026, 9, 18, 10, 0), (2026, 9, 18, 15, 0)],
            wall_values(slots),
        )

    def test_zero_videos_retorna_lista_vazia(self):
        self.assertEqual([], tiktok.build_slots(self.cfg, {}, 0, now_utc=FIXED_NOW_UTC))


class TiktokHorariosPorDiaTests(unittest.TestCase):
    """GATE 19.5 -- nova funcionalidade (horários por dia da semana):
    agendar_tiktok.py build_slots() ganha o mesmo suporte a
    horarios_por_dia que agendar_youtube.py build_slots() já tinha, no
    MESMO padrão (by_day.get(day_key, default_times)). Espelha
    YoutubeSlotTests acima para provar simetria real entre as duas
    plataformas, não só documentação."""

    def setUp(self):
        self.cfg = {
            "timezone_iana": "America/Sao_Paulo",
            "horarios": ["19:00", "20:00", "21:00"],
            "horarios_por_dia": {
                "quinta": ["20:00", "12:00", "17:00"],
                "sexta": ["12:00", "17:00", "20:00"],
            },
            "dias_janela": 9,
            "comecar_amanha_na_primeira_execucao": True,
        }

    def test_dias_com_entrada_especifica_usam_horarios_por_dia_ordenados(self):
        # FIXED_NOW_UTC = 16/09/2026 10:00 em SP (quarta) -> primeira
        # execução começa amanhã (17/09, quinta) -> usa horarios_por_dia
        # de "quinta" (ordenado, não na ordem em que foi digitado).
        slots = tiktok.build_slots(self.cfg, {"scheduled": []}, 4, now_utc=FIXED_NOW_UTC)
        self.assertEqual(
            [
                (2026, 9, 17, 12, 0),
                (2026, 9, 17, 17, 0),
                (2026, 9, 17, 20, 0),
                (2026, 9, 18, 12, 0),  # sexta também tem entrada específica
            ],
            wall_values(slots),
        )
        self.assertTrue(all(s.tzinfo == SAO_PAULO for s in slots))

    def test_dias_ausentes_do_dicionario_caem_no_fallback_horarios(self):
        # 19/09/2026 é sábado -- não está em horarios_por_dia -> usa o
        # fallback plano `horarios` (["19:00","20:00","21:00"]).
        state = {"scheduled": [{"datetime": "2026-09-18T20:00"}]}  # último = sexta 20:00
        slots = tiktok.build_slots(self.cfg, state, 3, now_utc=FIXED_NOW_UTC)
        self.assertEqual(
            [
                (2026, 9, 19, 19, 0),
                (2026, 9, 19, 20, 0),
                (2026, 9, 19, 21, 0),
            ],
            wall_values(slots),
        )

    def test_sem_horarios_por_dia_configurado_comportamento_identico_ao_de_antes(self):
        """Regressão explícita: uma conta TikTok SEM horarios_por_dia (o
        caso de toda conta já existente antes desta rodada) tem que
        continuar se comportando exatamente como TiktokSlotTests acima --
        mesmos horários, mesma ordem, nenhuma mudança de comportamento."""
        cfg_sem_por_dia = {
            "timezone_iana": "America/Sao_Paulo",
            "horarios": ["10:00", "15:00", "20:00"],
            "dias_janela": 9,
            "comecar_amanha_na_primeira_execucao": True,
        }
        slots = tiktok.build_slots(cfg_sem_por_dia, {"scheduled": []}, 4, now_utc=FIXED_NOW_UTC)
        self.assertEqual(
            [
                (2026, 9, 17, 10, 0),
                (2026, 9, 17, 15, 0),
                (2026, 9, 17, 20, 0),
                (2026, 9, 18, 10, 0),
            ],
            wall_values(slots),
        )


if __name__ == "__main__":
    unittest.main()
