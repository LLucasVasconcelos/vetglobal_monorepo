"""O byte NUL, em todo campo de texto que o cliente controla (D56).

`\\u0000` é escape válido de JSON, e coluna `text` do Postgres não guarda NUL. O
erro que isso levanta é `DataError` — **não** é `IntegrityError` —, escapa de todo
handler e volta `500`. Mesma família do id maior que a coluna e do nome de arquivo
longo demais.

O que torna este pior que os anteriores: `POST /auth/register` recebe texto livre
e **não exige token**. Antes desta correção, a API tinha um `500` alcançável sem
autenticação nenhuma.
"""

import pytest

from tests.conftest import PASSWORD, auth
from tests.test_internal_jobs import INTERNAL, NOTE, enqueue

NUL = "\x00"


@pytest.mark.parametrize(
    ("campo", "corpo"),
    [
        ("name", {"name": f"a{NUL}b", "owner_name": "Ana"}),
        ("owner_name", {"name": "Rex", "owner_name": f"a{NUL}b"}),
    ],
)
async def test_nul_em_pet_e_422_e_nao_500(client, aurora_token, campo, corpo):
    response = await client.post("/pets", json=corpo, headers=auth(aurora_token))

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"


async def test_nul_no_cadastro_e_422_sem_precisar_de_token(client):
    """A rota mais grave das cinco: texto livre, sem autenticação."""
    response = await client.post(
        "/auth/register",
        json={
            "tenant_name": f"Clinica{NUL}X",
            "email": "nul@nowhere.example.com",
            "password": PASSWORD,
        },
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"


async def test_nul_no_cadastro_nao_deixa_nada_pra_tras(client, db):
    from sqlalchemy import func, select

    from app.models import Tenant

    antes = await db.scalar(select(func.count()).select_from(Tenant))
    await client.post(
        "/auth/register",
        json={"tenant_name": f"a{NUL}b", "email": "nul2@nowhere.example.com", "password": PASSWORD},
    )
    await db.rollback()

    assert await db.scalar(select(func.count()).select_from(Tenant)) == antes


async def test_nul_na_senha_e_recusado_e_nao_truncado(client):
    """bcrypt é biblioteca C e trata a senha como string C: tudo depois de um NUL
    é ignorado em silêncio, então `abc\\0def` e `abc` seriam a mesma senha."""
    response = await client.post(
        "/auth/register",
        json={
            "tenant_name": "Clinica NUL",
            "email": "nul3@nowhere.example.com",
            "password": f"Vetglobal#{NUL}2026",
        },
    )

    assert response.status_code == 422


async def test_nul_no_login_nao_derruba_a_rota(client, aurora_email):
    response = await client.post(
        "/auth/login", json={"email": f"{aurora_email}{NUL}", "password": PASSWORD}
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("campo", "corpo"),
    [
        ("summary", {"status": "DONE", "summary": f"resumo{NUL}aqui"}),
        ("message", {"status": "FAILED", "error_code": "X", "message": f"a{NUL}b"}),
        ("error_code", {"status": "FAILED", "error_code": f"A{NUL}B"}),
    ],
)
async def test_nul_no_veredito_do_worker_e_422(client, aurora_token, campo, corpo):
    upload = await enqueue(client, aurora_token)
    await client.post("/internal/jobs/claim", headers=INTERNAL)

    response = await client.post(
        f"/internal/jobs/{upload['job_id']}/complete", json=corpo, headers=INTERNAL
    )

    assert response.status_code == 422


async def test_nul_no_nome_do_arquivo_e_422(client, aurora_token):
    """O nome não passa por Pydantic — vem na parte do multipart —, então a
    guarda precisa estar escrita à mão na validação do upload.

    O corpo é montado byte a byte de propósito: o `httpx` escapa o NUL ao
    construir o multipart, então um cliente educado **não reproduz** o achado.
    Foi preciso um cliente cru pra encontrá-lo, e é preciso um pra fixá-lo.
    """
    pet = await client.post(
        "/pets", json={"name": "Rex", "owner_name": "Ana"}, headers=auth(aurora_token)
    )
    corpo = (
        b'--X\r\nContent-Disposition: form-data; name="file"; filename="nota\x00.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n" + NOTE + b"\r\n--X--\r\n"
    )

    response = await client.post(
        f"/pets/{pet.json()['id']}/documents",
        content=corpo,
        headers={
            **auth(aurora_token),
            "Content-Type": "multipart/form-data; boundary=X",
        },
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "FILENAME_INVALID"


async def test_texto_normal_continua_passando(client, aurora_token):
    """A guarda recusa o byte NUL e nada além dele — acento, emoji e barra
    invertida são texto legítimo num nome de pet."""
    response = await client.post(
        "/pets",
        json={"name": "Açaí 🐕 \\ 'quote'", "owner_name": "Ana O'Brien"},
        headers=auth(aurora_token),
    )

    assert response.status_code == 201
    assert response.json()["name"] == "Açaí 🐕 \\ 'quote'"
