"""Observabilidade de job (D55): as duas durações, e quem segura a fila.

O que se fixa aqui não é desempenho — é que os números existem, que separam as
duas durações, e que o log de conclusão **não carrega prontuário**.
"""

import json
import logging

from app.models import JobStatus
from tests.conftest import auth
from tests.test_internal_jobs import INTERNAL, NOTE, enqueue

SUMMARY = "Vomiting for two days, hydrated, bland diet."


async def stats(client, top: int | None = None) -> dict:
    params = {"top_tenants": top} if top else {}
    response = await client.get("/internal/stats", params=params, headers=INTERNAL)
    assert response.status_code == 200
    return response.json()


async def test_stats_needs_the_internal_token(client, aurora_token):
    """Visão de todas as clínicas de uma vez — é a mesma razão das outras duas
    rotas internas: aqui não sobrou isolamento pra servir de rede (D27)."""
    assert (await client.get("/internal/stats")).status_code == 401
    assert (await client.get("/internal/stats", headers=auth(aurora_token))).status_code == 401


async def test_an_empty_queue_reports_zeros_and_not_nulls(client):
    body = await stats(client)

    assert body["jobs"] == {"ENQUEUED": 0, "PROCESSING": 0, "DONE": 0, "FAILED": 0}
    assert body["retried"] == 0
    assert body["waiting"]["samples"] == 0
    # Sem amostra não há média: `null` é honesto, `0.0` seria mentira.
    assert body["waiting"]["average_seconds"] is None


async def test_the_counts_follow_the_job_through_its_states(client, aurora_token):
    upload = await enqueue(client, aurora_token)
    assert (await stats(client))["jobs"]["ENQUEUED"] == 1

    await client.post("/internal/jobs/claim", headers=INTERNAL)
    assert (await stats(client))["jobs"]["PROCESSING"] == 1

    await client.post(
        f"/internal/jobs/{upload['job_id']}/complete",
        json={"status": "DONE", "summary": SUMMARY},
        headers=INTERNAL,
    )
    body = await stats(client)
    assert body["jobs"] == {"ENQUEUED": 0, "PROCESSING": 0, "DONE": 1, "FAILED": 0}


async def test_the_two_durations_are_measured_separately(client, aurora_token):
    """A separação é o ponto inteiro: espera diz "faltam workers", processamento
    diz "o trabalho ficou lento". Um número só não distingue os dois."""
    upload = await enqueue(client, aurora_token)
    await client.post("/internal/jobs/claim", headers=INTERNAL)
    await client.post(
        f"/internal/jobs/{upload['job_id']}/complete",
        json={"status": "DONE", "summary": SUMMARY},
        headers=INTERNAL,
    )

    body = await stats(client)

    assert body["waiting"]["samples"] == 1, "enqueued_at → claimed_at"
    assert body["processing"]["samples"] == 1, "claimed_at → finished_at"
    for clock in ("waiting", "processing"):
        assert body[clock]["average_seconds"] >= 0
        assert body[clock]["p95_seconds"] is not None


async def test_a_job_never_claimed_has_no_duration_to_report(client, aurora_token):
    """Média sobre linha que não tem o dado seria número inventado."""
    await enqueue(client, aurora_token)

    body = await stats(client)

    assert body["jobs"]["ENQUEUED"] == 1
    assert body["waiting"]["samples"] == 0


async def test_failures_are_counted_by_error_code(client, aurora_token):
    upload = await enqueue(client, aurora_token)
    await client.post("/internal/jobs/claim", headers=INTERNAL)
    await client.post(
        f"/internal/jobs/{upload['job_id']}/complete",
        json={"status": "FAILED", "error_code": "EXTRACTION_FAILED", "message": "boom"},
        headers=INTERNAL,
    )

    body = await stats(client)

    assert body["failures"] == {"EXTRACTION_FAILED": 1}
    assert body["jobs"]["FAILED"] == 1


async def test_busiest_tenants_answers_who_is_holding_the_queue(
    client, aurora_token, boreal_token
):
    """A fila é por ordem de chegada, então uma clínica com muito volume atrasa
    quem está atrás. Sem este número, ninguém sabe quem foi."""
    for n in range(3):
        await enqueue(client, aurora_token, content=NOTE + f" {n}".encode())
    await enqueue(client, boreal_token, content=NOTE + b" theirs")

    busiest = (await stats(client))["busiest_tenants"]

    assert len(busiest) == 2
    assert busiest[0]["in_flight"] == 3, "o mais carregado vem primeiro"
    assert busiest[1]["in_flight"] == 1


async def test_the_completion_log_carries_ids_and_never_the_record(
    client, aurora_token, caplog
):
    """O achado da D36 foi prontuário indo parar em log. Não volta pela porta da
    observabilidade: só identificador, estado e duração."""
    upload = await enqueue(client, aurora_token)
    await client.post("/internal/jobs/claim", headers=INTERNAL)

    with caplog.at_level(logging.INFO, logger="jobs"):
        await client.post(
            f"/internal/jobs/{upload['job_id']}/complete",
            json={"status": "DONE", "summary": SUMMARY},
            headers=INTERNAL,
        )

    linhas = [json.loads(r.message) for r in caplog.records if r.name == "jobs"]
    assert len(linhas) == 1
    registro = linhas[0]

    assert registro["event"] == "job_completed"
    assert registro["job_id"] == upload["job_id"]
    assert registro["status"] == JobStatus.DONE
    assert registro["waited_seconds"] is not None
    assert registro["processing_seconds"] is not None

    # E o que NÃO pode estar lá.
    assert SUMMARY not in caplog.text
    assert "consultation.txt" not in caplog.text


async def test_a_repeated_completion_does_not_log_twice(client, aurora_token, caplog):
    """O log acompanha a escrita, não a chamada: entrega repetida é normal
    (D18), e duplicá-la no log inflaria toda métrica derivada dele."""
    upload = await enqueue(client, aurora_token)
    await client.post("/internal/jobs/claim", headers=INTERNAL)
    payload = {"status": "DONE", "summary": SUMMARY}

    with caplog.at_level(logging.INFO, logger="jobs"):
        for _ in range(3):
            await client.post(
                f"/internal/jobs/{upload['job_id']}/complete", json=payload, headers=INTERNAL
            )

    assert len([r for r in caplog.records if r.name == "jobs"]) == 1
