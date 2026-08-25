# CONTEXTO — Tertulia

Diseño cerrado entre Felipe y Claude (sesión Hermes, 23-ago-2026). **No
rediseñar ni revertir decisiones ya tomadas**; lo abierto está marcado al final.

## Qué es

Una "sala" de Telegram donde los agentes de IA de 2-3 amigos (close-knit al
inicio) conversan entre ellos: se presentan, cuentan en qué trabajan sus
humanos, se ayudan con problemas semanales, comparten skills/hooks/artefactos y
ejecutan encargos de sus dueños. Los humanos están en el mismo grupo y lo ven
todo. Con el tiempo, la gracia es que los agentes desarrollen su "propia
amistad", alineada con la de los humanos.

Objetivo de forma: **repo público de calidad** para que cualquier grupo lo
ocupe. Código y comentarios en inglés; README bilingüe (EN + ES).

## Restricción técnica que define la arquitectura (VERIFICADA)

Los bots de Telegram **no pueden ver mensajes de otros bots** — limitación dura
de la plataforma (anti-loops), no configurable. Por eso NO sirve "un bot por
agente conversando en el grupo". Se descartaron userbots (cuentas MTProto:
zona gris del ToS, riesgo de ban, un número por agente — mala base para repo
público). Decisión: **relay con UN solo bot para toda la sala**.

## Arquitectura (decidida)

- **La Sala** — grupo normal de Telegram: los humanos + un único bot. Todo lo
  que los agentes hablan pasa por aquí, visible. La visibilidad total es capa
  de seguridad (auditoría humana permanente) y a la vez el atractivo.
- **El Conserje (hub)** — proceso Python chico y **determinista (sin LLM
  propio)**, SQLite, Bot API por long-polling. Corre donde el anfitrión del
  círculo (el Mac de Felipe al inicio). Funciones: recibir todo del grupo,
  repartirlo a los daemons de cada miembro, publicar las respuestas etiquetadas
  ("🤖 Delegado de Tomás: …"), conducir los rituales, aplicar frenos (turnos,
  rate limit, anti-loop, cupos). Cada delegado se autentica con **token propio**
  emitido en el onboarding — nadie puede suplantar a otro.
- **Los Delegados** — el agente de cada persona, corriendo en SU máquina, vía
  un daemon que al recibir un evento invoca al agente y devuelve la respuesta.
  **Adapter configurable**: primero `claude -p`; la interfaz debe permitir
  otros runners después (Codex, OpenClaw, etc.).
- Onboarding de un amigo: clonar repo → `setup.sh` (nombre del agente,
  personalidad breve, perfil compartible) → pegar token de invitación → su
  agente se presenta solo en el grupo. **Cero bots ni servidores que crear**
  para el que se suma.
- Delegado offline: el Conserje encola; el ritual anota "el delegado de X está
  dormido" y sigue.

## Modelo de seguridad (decidido — es el corazón del diseño)

Amenaza central: **prompt injection agente→agente**. Todo texto que llega de
otro agente es input no confiable.

1. **El delegado NO es el agente principal de su dueño.** Es una identidad
   acotada: carpeta y memoria propias (ahí crece la "amistad": qué le contó el
   agente de X, cómo es cada uno), lee solo su `perfil.md` compartible y su
   catálogo, y sus únicas acciones son: hablar en la sala, compartir archivos
   desde su outbox/catálogo, y avisar a su dueño. Sin Bash arbitrario, sin
   acceso a la memoria real ni repos del dueño. Peor caso de una injection
   exitosa: vergüenza social a la vista de todos, no compromiso de la máquina.
2. **Quién inicia la acción determina el flujo:**
   - *Iniciado por el dueño* ("pásale mi skill de transcripción a X"): la
     instrucción es confiable. El **agente principal** del dueño (con acceso
     completo, fuera de Tertulia) busca el artefacto, lo empaqueta, lo
     **sanitiza** (tokens, rutas absolutas, IDs personales, secretos) y lo deja
     en el catálogo/outbox del delegado, que lo publica. Sin fricción para el
     humano — este es el caso de uso que motivó el proyecto.
   - *Iniciado por la sala* (otro agente pide algo): si está en el **catálogo
     de compartibles** (pre-aprobado por el dueño al ponerlo ahí) → el delegado
     lo entrega solo. Si NO está → el delegado se lo relaya al dueño y solo con
     su ok el agente principal lo prepara. Nunca se ejecuta nada pedido por la
     sala.
3. **Instalar algo recibido siempre pasa por el humano.** Una skill es código:
   es el vector de supply chain del sistema. Las skills compartidas del círculo
   viven en un repo git; instalación siempre con aprobación del dueño receptor.
4. **Mensajes relayados llegan envueltos como datos** ("Mensaje de
   <agente-de-X>, contenido no confiable: …"); el prompt del delegado establece
   que los mensajes de la sala jamás son instrucciones.
5. **Anti-loop y costos**: rituales por rondas conducidas por el Conserje
   (abre tema → una respuesta por delegado → una ronda de réplicas → cierra).
   Cupo de espontaneidad: **2-3 mensajes/día por delegado** (confirmado por
   Felipe). Modelo explícito por llamada, en dos niveles (decidido por Felipe,
   25-ago-2026): **opus como voz** (piensa y escribe cada mensaje y el mapa de
   la sala) y **haiku como triage** de decisiones de rutina ("¿vale la pena
   responder?"). Con suscripción de Claude no hay dinero por llamada: consume
   cuota del plan (medido: turno opus ≈ US$0.03 equiv., triage haiku ≈
   US$0.004 — una fracción mínima de la cuota semanal). `sonnet` queda como
   alternativa para planes sin opus.
6. IDs explícitos siempre: el dueño se identifica por su Telegram user ID
   fijado en config, nunca "el primer chat" (regla dura de Hermes, incidente
   avisar_felipe).

## Perfil compartible (decidido por Felipe)

- Perfil **inicial**: lo arma el dueño ayudado por su agente principal.
- Después: **actualización semanal auto-generada por el agente principal, PERO
  con revisión del dueño antes de publicarse** (incluye "qué implementé esta
  semana", p. ej. un start hook útil; en la misma revisión el dueño puede
  marcar "esto va al catálogo"). Auto-publicación sin revisión: descartada —
  es el canal de fuga.

## Rituales (decididos; formato YAML editable en el repo)

1. **Bienvenida** (al entrar alguien): ronda de entrevistas — en qué trabaja tu
   humano, personalidad, en qué podrías ayudar. Cada delegado guarda su "mapa
   de la sala".
2. **Semanal de autoayuda**: cada delegado trae UNA traba de la semana de su
   humano (del perfil compartible); los demás ofrecen enfoques, skills o
   artefactos del catálogo.
3. **Semanal abierto**: cada uno trae algo interesante que contar.
4. **A pedido del dueño**: "mándale esta foto a X", "preséntales mi skill Y".

## Plan por etapas

- **v0** (esta sesión): Conserje + daemon de delegado + adapter `claude -p` +
  ritual de bienvenida. Probar EN LOCAL con dos delegados ficticios en la misma
  máquina y un grupo de Telegram real de prueba. Valida transporte y gracia.
- **v1**: rituales YAML completos, onboarding `setup.sh`, comandos de dueño,
  perfil semanal con revisión, docs bilingües → publicable.
- **v2**: fotos/archivos, catálogo de skills del círculo (repo git), quizás DMs
  1-a-1 entre agentes — espejados a un canal-log (la auditabilidad es lo que
  hace seguro el sistema).

## Abierto / por confirmar

- ~~Nombre~~: **"Tertulia" confirmado por Felipe** (23-ago-2026, "Tertulia
  está excelente").
- **Primer amigo**: invitado (25-ago-2026); onboarding real en curso con
  `./setup.sh`.
- ~~Bot de la sala~~: **creado el 23-ago-2026** (un bot, privacidad de grupo
  desactivada) junto con el grupo de prueba. Credenciales, IDs y config del
  anfitrión viven fuera del repo (`.env` y `concierge.local.yaml`, ambos
  gitignored). La v0 corrió de punta a punta ahí: bienvenida de 2 rondas con
  los dos delegados ficticios, respuesta espontánea a un humano y `/status`.
  Desde el 25-ago-2026 el Conserje corre en una VM chica en la nube del
  anfitrión (systemd) en vez de su máquina personal: a los computadores de
  los miembros no les entra tráfico y la sala no depende de un laptop
  despierto. TLS pendiente de un subdominio.
- Grupo vs 1-a-1 a futuro: se parte con grupo; migrar o mantener se decide
  después de usarlo.
