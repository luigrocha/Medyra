"""LLM extractor: HTML/texto → estructura tipada vía Claude.

Diseño:
  - Pydantic schema = contrato. Si el modelo se sale del schema, hard-fail.
  - Prompt sistema cacheable (~2KB), prompt usuario variable (~500 tokens).
  - Modelo default: claude-haiku-4-5 (barato, suficiente para extracción).
  - Anthropic SDK opcional: si no hay `ANTHROPIC_API_KEY`, el extractor levanta
    excepción explícita en lugar de fallback silencioso.

Uso:
    extractor = LLMExtractor()
    result = await extractor.extract(profile_html, country="EC", hint_name="...")
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from medyra.config import settings


class ExtractedContact(BaseModel):
    """Un contacto inferido del HTML por el LLM."""
    kind: str = Field(description="email | phone | website")
    value: str
    subkind: str | None = Field(default=None, description="personal | clinic | hospital | unknown")
    source_text: str = Field(description="Fragmento del HTML/texto donde lo encontró")


class ExtractedAffiliation(BaseModel):
    organization: str
    role: str | None = None
    city: str | None = None


class LLMExtractionResult(BaseModel):
    found_doctor: bool = Field(description="True si el HTML parece corresponder a un perfil de médico")
    full_name: str | None = None
    specialty: str | None = None
    contacts: list[ExtractedContact] = Field(default_factory=list)
    affiliations: list[ExtractedAffiliation] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    confidence_self: float = Field(ge=0.0, le=1.0, default=0.5, description="Auto-rating del LLM sobre la calidad de la extracción")
    notes: str | None = None


SYSTEM_PROMPT = """\
Eres un extractor de datos médicos. Recibes texto/HTML de una página web pública
(perfil de médico, página de clínica, resultado de búsqueda) en español.

Tu tarea: extraer información estructurada del médico mencionado.

Reglas estrictas:
1. Solo extraes lo que está LITERALMENTE en el texto. No inventes datos.
2. Si dudas si un teléfono/email es del médico o de una clínica genérica, márcalo como subkind="clinic".
3. Si la página no es de un médico (404, listado, otro), devuelve found_doctor=false.
4. Para confidence_self: 0.9 si todo está explícito; 0.6 si inferido de contexto; 0.3 si dudoso.
5. NUNCA devuelvas teléfonos sin código de país detectable.

Devuelve solo JSON válido conforme al schema. Sin texto adicional, sin markdown.
"""


class LLMExtractor:
    def __init__(self, model: str | None = None) -> None:
        self.model = model or settings.llm_model
        if not settings.anthropic_api_key:
            self._client = None
        else:
            try:
                from anthropic import AsyncAnthropic
                self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)
            except ImportError:
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    async def extract(
        self,
        text: str,
        *,
        country: str,
        hint_name: str | None = None,
        hint_specialty: str | None = None,
        max_tokens: int = 1500,
    ) -> LLMExtractionResult:
        if self._client is None:
            raise RuntimeError(
                "LLMExtractor no disponible: ANTHROPIC_API_KEY no está set o "
                "anthropic SDK no instalado (`pip install -e '.[llm]'`)."
            )

        # Truncar a ~12k chars para mantener costo bajo. Selectolax podría
        # extraer solo texto antes para mayor densidad informativa.
        snippet = text[:12000]
        hints = []
        if hint_name:
            hints.append(f"Médico esperado: {hint_name}")
        if hint_specialty:
            hints.append(f"Especialidad esperada: {hint_specialty}")
        hints.append(f"País: {country}")
        hint_block = "\n".join(hints)

        user_msg = (
            f"{hint_block}\n\n"
            f"=== PÁGINA ===\n{snippet}\n=== FIN ===\n\n"
            f"Responde SOLO con JSON conforme a este schema:\n"
            f"{json.dumps(LLMExtractionResult.model_json_schema(), indent=2)}"
        )

        # Prompt caching: el system prompt es estable y rentable de cachear.
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=[
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
            ],
            messages=[{"role": "user", "content": user_msg}],
        )
        raw: str = ""
        for block in response.content:
            if getattr(block, "type", None) == "text":
                raw += block.text  # type: ignore[attr-defined]

        return self._parse_strict(raw)

    @staticmethod
    def _parse_strict(raw: str) -> LLMExtractionResult:
        """Parsea JSON estricto; tolera markdown-wrappers comunes."""
        s = raw.strip()
        if s.startswith("```"):
            # ```json\n...\n``` o ```\n...\n```
            s = s.strip("`")
            if s.lstrip().startswith("json"):
                s = s.lstrip().removeprefix("json").lstrip()
            s = s.rstrip("`").strip()
        data: dict[str, Any] = json.loads(s)
        return LLMExtractionResult.model_validate(data)
