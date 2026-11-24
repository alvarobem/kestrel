# Fase 2 — planificación real con transporte LLM por asiento personal

Objetivo: que los nodos de la fase 2 puedan pedirle razonamiento a un modelo, con
el asiento de GitHub Copilot del desarrollador y sin API keys.
Rama: `feat/llm-copilot-seat` (basada en `feat/telemetry-provider-port`, que aún no está en `main`).
Estrategia de entrega: un PR encadenado al de fase 1; se rebasa cuando fase 1 mergee.
Runner: `make test` (pytest en la imagen, árbol montado) y `make lint` (ruff).

## Specs

**S1 — El transporte LLM existe como puerto, y hoy lo responde un asiento personal de Copilot.**
Pedido textual del usuario:

> "quiero empezar la fase 2 pero para la integracion con LLMs me gustaria poder integrar subscripciones activas (por ejemplo copilot) como por ejemplo hace opencode. Esto sería viable, podriamos usar subscripciones activas para los LLMs de esta herramienta en vez de api_keys que cuestan creditos?"

> "quiero es seat personal"

Criterio: `LLM_PROVIDER=copilot` + device OAuth de GitHub; sin `LLM_API_KEY` y sin
GitHub App de organización. El adaptador se elige en un registro, igual que
`providers.py` para telemetría.

**S2 — El núcleo no nombra al vendor.**
`domain/` y `application/` no importan `litellm` ni dicen "copilot"; la capacidad
llega por `CoreDeps.llm`. Criterio: `tests/test_architecture.py` sigue pasando y
`nodes.py` no menciona al vendor.

**S3 — Sin asiento configurado, la corrida degrada y dice por qué.**
`build_deps` devuelve `llm=None` y `llm-check` sale con código 2 nombrando el
comando que falta. Es la misma regla que la telemetría: nunca reventar el arranque
por una credencial ausente.

**S4 — Construir el cliente no hace red ni abre un device flow.**
Solo un comando explícito (`llm-login`, `llm-check`) dispara OAuth. Criterio: un
test construye el cliente con el token presente y no toca la red.

**S5 — El token del asiento sobrevive a recrear contenedores.**
Volumen nombrado compartido entre el `docker compose run` del login y el worker.
Criterio: `make llm-login` una vez, después `docker compose exec` lo reutiliza.

**S6 — Sin regresiones.**
`make lint` y `make test` en verde, incluido `test_architecture.py`.

Fuera de alcance de esta rebanada: `triage`, `plan` y `report` todavía no llaman al
modelo. Esa es la tarea siguiente y necesita decisión de diseño (forma del prompt,
salida estructurada, qué se hace con una respuesta inválida).

## Tasks

- T1 · S1,S2 · inline · hecho · puerto `LlmClient` + `LlmUnavailable` + `CoreDeps.llm`
- T2 · S1,S3,S4 · inline · hecho · `adapters/outbound/llm.py`: registro + cliente Copilot
- T3 · S1 · inline · hecho · `config.py` + compose (env y volumen) + `.env.example`
- T4 · S1,S3 · inline · hecho · CLI `llm-check` / `llm-login` + targets de Makefile
- T5 · S6 · inline · hecho · `make lint` limpio; `make test` 154 passed, 1 skipped
- T6 · S4 · humano · **pendiente** · el usuario completa `make llm-login` (device flow, requiere su navegador)

## Log

**L1 (2026-11-24) — pedido original, textual:**

> "quiero empezar la fase 2 pero para la integracion con LLMs me gustaria poder integrar subscripciones activas (por ejemplo copilot) como por ejemplo hace opencode. Esto sería viable, podriamos usar subscripciones activas para los LLMs de esta herramienta en vez de api_keys que cuestan creditos?"

**L2 (2026-11-24) — decisión de transporte, textual:**

> "quiero es seat personal"

**L3 — evidencia que sostiene el diseño.** LiteLLM documenta `github_copilot/...`
con device OAuth y caché de token en `~/.config/litellm/github_copilot/access-token`
(override: `GITHUB_COPILOT_TOKEN_DIR`). GitHub documenta además el camino
server-to-server (GitHub App con `organization_copilot_requests: read`) para
servicios; ese es el transporte de producción y reemplaza este adaptador, no lo
configura. Los "subscription" de Claude/ChatGPT en LiteLLM pilotan el CLI del
vendor, no son una API: descartados para un worker.

**L4 — por qué litellm y no un cliente propio.** El device flow y el intercambio
de token son la parte que cambia contra GitHub; litellm ya los mantiene. El
adaptador queda en una sola archivo, con import perezoso, para que el día que se
rompa haya un lugar donde mirar.

**L5 — gotcha verificado contra el runtime, no supuesto.** `litellm` se niega a
arrancar el device flow dentro de un event loop:

> `GitHub Copilot device-code login needs a human and cannot run inside a running event loop or a worker thread`

Es correcto: la espera es de un humano, y dentro de una activity bloquearía el
worker entero. Por eso `sign_in` es **síncrona** y el CLI la invoca sin
`asyncio.run`; `test_sign_in_is_synchronous_so_it_can_wait_for_a_human` lo fija
para que nadie lo "arregle" volviéndola coroutine.

**L6 — evidencia de esta rebanada.**
- `make lint`: All checks passed.
- `make test`: 154 passed, 1 skipped (incluye `test_architecture.py`).
- `llm-check` sin token: imprime el modelo configurado y `seat token: missing at
  /data/copilot/access-token — run 'make llm-login' once`, exit 2.
- `llm-login` (ejecutado 35s y cortado): imprimió
  `Please visit https://github.com/login/device and enter code 8237-763A to
  authenticate.` ← el camino llega a GitHub; falta solo la aprobación humana (T6).
- Construir el cliente con token presente: sin socket (test con `socket.socket`
  parcheado a fail).

**L7 — pendiente de decisión del usuario.** `LLM_MODEL=github_copilot/gpt-4.1` es
un default; qué modelos ofrece la cuenta lo decide el plan de Copilot. Si
`llm-check` responde "model not available", se cambia ese valor y nada más.
