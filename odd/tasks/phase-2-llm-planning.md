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
- T6 · S4 · humano · **hecho** · login completado por el usuario; la seat responde

**Estado: la rebanada está cerrada.** El puerto, el adaptador, la degradación, el
volumen y los dos comandos funcionan contra la seat real. La tarea siguiente es el
nodo `plan`.

## Specs (segunda rebanada — el nodo `plan`)

**S7 — El modelo propone, el nodo dispone.** `plan` pide un plan al puerto `LlmClient`,
lo parsea y lo pasa por guardarraíles puros (`application/planner.py`). Lo que no
sobrevive no llega nunca a un humano.

**S8 — Los guardarraíles son del dominio, no del prompt.** Un paso con wildcard, con
`--all`/`-A`, sin target, o irreversible sin `rollback_command` se rechaza. El
`requires_approval` que devuelve el modelo se ignora: la política es nuestra.

**S9 — Declinar es una respuesta válida.** Si la evidencia no sostiene una acción, el
modelo devuelve `steps: []` y su explicación se convierte en la razón registrada. No
se reintenta: reintentar repetiría la misma respuesta y costaría otra llamada.

**S10 — El prompt no filtra payloads.** Solo viaja `Evidence.summary`; `payload` (logs
crudos, cabeceras con tokens) se queda en casa. Hay un test que lo fija.

**S11 — Un plan degradado nunca pide aprobación.** Sin plan, la corrida va a `report`
con la razón como evidencia. `make smoke` explica ese desenlace en vez de culpar al
worker.

**S12 — BLOQUEANTE ABIERTO: la superficie de remediación no está declarada.** Ver L10.

## Tasks (segunda rebanada)

- T7 · S7,S8 · inline · **hecho** · `application/planner.py`: prompt, parseo, guardarraíles
- T8 · S7,S9,S11 · inline · **hecho** · nodo `plan` con reparación acotada a un intento
- T9 · S11 · inline · **hecho** · `smoke` explica "terminó sin pedir aprobación" y por qué
- T10 · S7-S11 · inline · **hecho** · `test_planning.py` (15 casos) + `StubLlmClient`
- T11 · S12 · inline · **hecho** · el vocabulario se declara como capacidades, no como verbos de kubectl

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

**L8 (2026-11-24) — modelo por defecto, textual del usuario:**

> "ya he hecho login, pero el modelo por degefto debe ser GPT-6 Luna"

Resuelto sin adivinar: el catálogo instalado (litellm 1.110.3) expone 68 ids
`github_copilot/*`, entre ellos `gpt-6-luna`, `gpt-luna-latest`, `gpt-6-sol`,
`gpt-6.1-sol`, `gpt-6.2-sol`. Una llamada real con la seat confirmó que
`github_copilot/gpt-6-luna` responde. Ese es el default en los cuatro lugares
donde vive el valor: `config.py`, `docker-compose.yml`, `.env.example`, `.env`.

Evidencia final, con el código de este repo:

```
$ make llm-check
llm provider: copilot (personal seat, device OAuth)
llm model:    github_copilot/gpt-6-luna
seat token:   present at /data/copilot/access-token
answer:  kestrel llm ok
latency: 3.6s
```

Nota: `llm-check` tarda segundos, no milisegundos, porque cada invocación levanta
litellm y abre conexión. Para los nodos de fase 2 eso es irrelevante — son
actividades con timeout propio — pero no se debe medir calidad de razonamiento con
esa latencia.

**L9 — el nodo `plan` funciona; el modelo se niega a inventar.** Dos corridas reales
con `github_copilot/gpt-6-luna`, mismo código:
1. Evidencia "1.0x, steady" → `the evidence did not support any action: No
   remediation is proposed because the available evidence does not identify a cause`.
2. Davis problem + `OutOfMemoryError` (x412) + failure ratio 0.83 vs 0.01 →
   `The evidence points to heap exhaustion, but lacks the runtime and deployment
   details needed to choose a safe, targeted remediation command`.

La segunda es la importante: con evidencia que **sí** sostiene una acción, el modelo
declinó por no conocer cluster, namespace ni nombre de deployment. No inventó
`kubectl rollout restart deploy/foo` con un nombre plausible. Un modelo menos
cuidadoso habría producido un plan que parece perfecto y apunta al objeto
equivocado — el peor resultado posible para una herramienta que ejecuta acciones.

**L10 — BLOQUEANTE (S12): falta declarar la superficie de remediación.** El prompt
no puede afirmar capacidades que el sistema no tiene (el sandbox sigue sin diseño,
§13), así que el modelo no sabe qué vocabulario de acciones es legal. Mientras no se
declare, `plan` degradará casi siempre con una razón correcta pero inútil para el
on-call. Dos caminos, decisión del usuario:- **A. Vocabulario cerrado**: el modelo propone `RESTART_DEPLOYMENT`/`SCALE`/
  `ROLLBACK` sobre un target, y el ejecutor renderiza el comando. Elimina la clase
  entera de "el modelo inventó un flag"; los guardarraíles pasan a ser estructurales.
- **B. Superficie declarada**: el prompt recibe el contexto real (herramienta,
  cluster, namespace, nombres válidos) como valor de política en `CoreDeps`, y el
  modelo escribe el comando dentro de ese vocabulario.

Evidencia del desenlace degradado, con el stack completo:

```
$ make smoke
the run finished without asking for approval (status=COMPLETED)
final phase=failed
  - investigate degraded: dynatrace returned no evidence for this incident inside the run scope
  - plan degraded: the evidence did not support any action: ...
  - approval gate reached with no plan
  - report generated (resolved=False)
```

**L11 (2026-11-24) — corrección del usuario, textual:**

> "piensa que esto no lo usare solo para oncall, imaginate que deja de arrancar un servicio en integracion por un fallo de código. No puede ser tan cerrado y pensando solo en on call. Ten esto en cuenta"

Corrección aceptada: la primera versión de B era `tool` único + lista de verbos +
namespace obligatorio, que es kubectl-y-on-call disfrazado. Rediseñado como **lista
de capacidades** (`Capability`: `id`, `tool`, `verbs`, `must_contain`,
`description`). El paso cita la capacidad que usa, y el guardarraíl comprueba
herramienta, verbo y los literales que fijan dónde aterriza la acción. Nada en el
dominio sabe qué es un namespace, un pipeline o una rama: el vocabulario lo declara
el operador.

Declaración en `.env` (formato: `id|tool|verbs|must_contain|description`, `;` entre
capacidades). Producción on-call y un build roto en integración, mismo mecanismo:

```
REMEDIATION_CAPABILITIES=restart_workload|kubectl|rollout,scale,delete|-n checkout|restart, scale or delete one workload in checkout;read_workload|kubectl|get,describe,logs|-n checkout|read the state or logs of one workload in checkout
```

Evidencia con el modelo real y la superficie declarada:

```
declared: [('restart_workload','kubectl',('rollout','scale','delete'),('-n checkout',)),
           ('read_workload','kubectl',('get','describe','logs'),('-n checkout',))]
notes:    plan proposed by github_copilot/gpt-6-luna: 1 step(s), hypothesis confidence=0.90
hypothesis: OrderHandler is returning 5xx because it is exhausting its Java heap. (0.9)
  1. [read_workload] kubectl logs deployment/order-handler -n checkout --tail=100
     target=order-handler reversible=True risk=low
```

Dos observaciones honestas:
1. Con la superficie declarada, el modelo dejó de declinar y propuso un plan válido.
2. Eligió **leer logs** antes que reiniciar. Es defendible (mirar antes de tocar),
   pero un plan cuyo único paso es de lectura no remedia nada: si el operador no
   quiere eso, no declara capacidades de solo lectura y el modelo queda forzado a
   elegir una acción que cambia algo.

Pendiente real, no bloqueante: `Capability` no se filtra por entorno. Hoy un
despliegue de integración y uno de producción declaran superficies distintas en su
propio `.env`; hacerlo por incidente necesita que `Incident` lleve el entorno, que
el código ya insinúa con el entity id.

**L12 (2026-11-24) — segunda corrección del usuario, textual:**

> "aqui hay algo que no cuadra, leer los logs los deberia haber leido ya en el paso de investigation. Los logs ya los tiene y los eventos. Con eso tiene que tomar una decisión que sea acciones concretas. [...] el plan no puede ser de salida leer logs, porque para eso ya está el agente, no aporta absolutamente nada"

Defecto aceptado: yo lo había tratado como decisión de configuración ("si no
quieres pasos de lectura, no declares la capacidad"). Es un defecto de diseño: el
sistema *invitaba* a proponer un no-op. Corregido en tres frentes:

1. `Capability.effect` (`change` | `read`), campo 5 de la declaración.
2. Regla estructural en `require_usable`: un plan compuesto solo de capacidades
   `read` se rechaza (`the plan only reads information the investigation already
   gathered`). Un paso de lectura junto a uno de cambio sigue permitido.
3. El prompt lo dice sin ambigüedad: `The evidence is final. You cannot investigate
   further [...] Every plan needs at least one step that changes the system.`

Evidencia antes/después, misma evidencia, mismo modelo:

```
antes:   1. [read_workload] kubectl logs deployment/order-handler -n checkout --tail=100
después: 1. [restart_workload] kubectl rollout restart deployment/order-handler -n checkout
            rollback=kubectl rollout undo deployment/order-handler -n checkout
            hypothesis confidence=0.88
```

`.env` y `.env.example` quedaron con `read_workload` marcada `read`, para que la
regla sea visible en la configuración y no solo en el código.
