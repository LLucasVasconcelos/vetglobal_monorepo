from fastapi import FastAPI

app = FastAPI(
    title="VetGlobal Backend",
    description="Asynchronous clinical document summarization API",
    version="0.1.0",
)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Liveness probe. Does not touch the database."""
    return {"status": "ok"}
