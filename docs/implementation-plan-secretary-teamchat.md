# Implementatieplan: stabiliteit afmaken, Secretary laten delegeren, team chat

Datum: 2026-08-15
Status: **plan, nog geen code gewijzigd**
Vervolg op: `docs/handover-approval-hang-fix.md` (root causes van de hang-bugs die al gefixt zijn
in `a31a920`/`12e2f9c`) en de investigate-sessie op branch `agent/fix-approval-escalation-hangs`.

## Aanleiding

Drie klachten van de gebruiker (CEO-persona): (1) chats lopen soms vast zonder feedback,
(2) de Secretary voelt nutteloos aan, (3) er is geen manier om als team (CEO + rollen) met
elkaar te chatten. Onderzoek (3 parallelle deep-dives, zie sessie van 2026-08-14) wees uit:

- De grootste resterende hang-oorzaak is een **reconciliatie die alleen draait als iemand
  toevallig de sessie heropent** — geen proactieve sweep.
- De Secretary is **architectonisch geen deel van de organisatie**: los kanaal, geen
  verbinding naar `OrgEngine`/`CollaborationService`/work items, enige actie is
  `import_skill`.
- Een multi-party "team chat" (CEO + meerdere rollen, zelfde thread) **bestaat niet**, wel
  bruikbare bouwstenen (`activity:{project_id}` kanaal, `AgentMessage.to_agents` fan-out,
  `MeetingRoom`).

Dit plan werkt dat uit in concrete, sequentiële implementatiestappen. Volgorde is bewust:
laag risico/hoogste klachten-impact eerst, de risicovolle identity-refactor bewust laatst
en apart (gezien de fix/revert-geschiedenis daarop: `14ee880`, `326d305`, `12817a4`,
`c901800`, `57610e5`).

---

## Fase 1 — Stabiliteit afmaken

### 1.1 Proactieve sweep voor verweesde checkpoints/escalaties (hoogste prioriteit)

**Probleem**: `CommsReactivationSweeper` (`opc/layer2_organization/reactivation_sweeper.py`)
scant elke 10s alleen `TaskStatus.DONE`-taken en mail-reactivatie-guards. Reconciliatie van
`ExecutionCheckpoint`/`human_escalation`-kaarten (`_reconcile_inactive_human_escalation_cards`,
`_reconcile_execution_checkpoint_cards`, `opc/plugins/office_ui/ws_handler.py:5887-5896`)
draait uitsluitend binnen de `session_detail`-WS-handler, dus alleen bij handmatig
heropenen van een sessie.

**Aanpak**:
1. Extraheer de kern van `_reconcile_inactive_human_escalation_cards` en
   `_reconcile_execution_checkpoint_cards` uit `ws_handler.py` naar een functie die niet
   afhankelijk is van een actieve WS-connectie/session-context (puur op `store`/`chat_store`
   werkt).
2. Voeg een nieuwe periodieke pass toe — als losse coroutine naast
   `CommsReactivationSweeper`, of als extra scan binnen dezelfde sweeper-cyclus (voorkeur:
   losse klasse `CheckpointReactivationSweeper` voor scheiding van verantwoordelijkheid,
   zelfde interval-patroon van 10s hergebruiken).
3. Scope: alle projecten/sessies met een open checkpoint/escalatie ouder dan een
   drempelwaarde (bv. 30s, om races met net-aangemaakte kaarten te vermijden), niet alleen
   de sessie die toevallig open staat.
4. Bij een gevonden orphan: dezelfde reconciliatie-actie als vandaag bij handmatige reload
   (kaart opnieuw mirroren/markeren), plus een log-regel zodat dit zichtbaar is in
   `.opc/logs/`.

**Tests**: uitbreiden van `tests/test_stale_checkpoint_selfheal.py` met een scenario zonder
sessie-reload (puur de nieuwe achtergrond-sweep laten triggeren). Regressietest dat de
bestaande reload-pad in `ws_handler.py` ongewijzigd blijft werken.

**Risico**: laag — puur additief, raakt geen bestaande routing-logica.

### 1.2 Resterend `progress_log`-schrijfpad naar `task.metadata` opsporen

**Probleem**: ook na de `a31a920`-fix (die checkpoint-resume via de WorkItem-owned
metadata-updater liet lopen) blijven `metadata_ownership_conflict`-warnings verschijnen in
de logs van vandaag voor `progress_log`. Er is dus minstens één ander codepad dat nog
rechtstreeks `task.metadata["progress_log"]` schrijft.

**Aanpak**:
1. `grep -rn "progress_log" opc/` en alle schrijf-sites inventariseren die niet via
   `append_work_item_progress`/`update_work_item_owned_metadata`
   (`opc/layer2_organization/metadata_ownership.py:524-549`) lopen.
2. Elke gevonden site ombouwen naar de WorkItem-owned helper, analoog aan de fix in
   `a31a920`.
3. Verifiëren met een run die eerder een conflict triggerde (zelfde run-vorm als
   `run_id=1d84f824…`/`17bf068b…` uit de logs van 2026-08-14) en bevestigen dat de warning
   niet meer verschijnt.

**Tests**: uitbreiden van de bestaande metadata-ownership-testsuite
(`tests/test_metadata_ownership.py`, `tests/test_work_item_runtime_invariants.py`) met het
nieuw gevonden codepad.

**Risico**: laag — gedrag blijft functioneel identiek (WorkItem won toch al bij conflicten),
dit maakt alleen de warning overbodig.

### 1.3 `company_runtime_identity`-classificatie fixen (apart, voorzichtig, later)

**Probleem**: `_has_company_runtime_marker`
(`opc/layer2_organization/company_runtime_identity.py:82-88`) classificeert een Task als
company-runtime zodra hij een `company_profile`-veld draagt, ook zonder expliciete
task-mode marker (legacy fallback). Tweede pad: `is_pure_company_ui_anchor`
(regels 97-126) vereist een exacte `session_id`-match; na een resume die de runtime
session-id laat verschuiven t.o.v. de originele anchor-Task matcht niets meer en
`ui_anchor_task_id` valt leeg terug.

**Aanpak** (bewust pas na 1.1/1.2, en met eigen review-ronde):
1. Eén canonieke marker-set vastleggen voor "is company-owned" los van "is de UI-anchor" —
   de bare `company_profile`-fallback intrekken of scherper scopen (bv. alleen als
   aanvullend signaal, nooit als enige voorwaarde).
2. Voor `ui_anchor_task_id`: een ancestor-chain-fallback toevoegen (via
   `linked_work_item_id_for_task`/parent-Task-keten) in plaats van bij een mismatch leeg
   terug te vallen.
3. Uitgebreid regressietesten tegen de eerdere revert-commits (`14ee880`, `326d305`,
   `12817a4`, `c901800`, `57610e5`) — dit gebied heeft een geschiedenis van "fix hier, breekt
   daar".

**Tests**: nieuwe unit tests voor `is_company_runtime_task`/`_has_company_runtime_marker`/
`is_pure_company_ui_anchor` met de exacte edge cases uit de handover (legacy `company_profile`
zonder task-mode marker; resume met gewijzigde session-id). Volledige
`tests/test_ws_handler_escalations.py`-suite als regressienet.

**Risico**: middel — vandaar apart en laatst binnen Fase 1.

---

## Fase 2 — Secretary laten delegeren

**Uitgangspunt**: `SecretaryService` (`opc/layer2_organization/secretary.py`) blijft de
persoonlijke, project-brede assistent (voorkeuren, policy-uitleg, skill-imports), maar
krijgt er een echte delegatie-actie bij, zodat "praat met je secretary, zij zet het team
aan het werk" ook daadwerkelijk gebeurt.

### 2.1 Delegatie-primitief kiezen

`opc/layer2_organization/org_work_item_planner.py` (work item aanmaken/plannen) en
`opc/layer2_organization/collaboration_service.py` (`send_dm`, `propose_task_adjustment`)
zijn de twee kandidaten. Voorkeur: werk-item-aanmaak via `org_work_item_planner`, omdat dat
de gebruikelijke ingang is waarmee `delivery_lead` (de facto CEO-rapportagelijn in
`org_splinter_config.yaml`) al werkt oppakt — een DM zou een side-channel buiten het normale
work-item-dispatch-pad creëren.

### 2.2 Actie-schema uitbreiden

1. In `secretary.py`: naast `import_skill` een nieuwe actie `delegate_to_team` (of
   vergelijkbaar) toevoegen aan `_apply_actions` (regels 184-228), met velden zoals
   `{"summary": str, "target_role": str | None, "priority": str | None}`.
2. `target_role` optioneel maken — als leeg, laat de planner/`delivery_lead` zelf bepalen
   wie het oppakt (auto-route, zoals `delivery_lead.coordinator_policy` al ondersteunt).
3. Implementatie van de actie: roept `org_work_item_planner` aan om een work item te maken
   gekoppeld aan het huidige project, met de samenvatting van het CEO-verzoek als
   omschrijving, en linkt de bron (secretary-sessie) in de metadata zodat een latere
   statusvraag ("hoe staat het ermee?") terug te herleiden is.
4. Terugkoppeling: de secretary-response bevat een korte bevestiging + work-item-referentie
   ("ik heb dit doorgezet naar tech_lead als taak X"), zodat de CEO altijd een concreet
   aanknopingspunt heeft.

### 2.3 System prompt bijwerken

`_system_prompt` (`secretary.py:136-162`) uitbreiden: wanneer delegeren (concrete,
uitvoerbare verzoeken) versus wanneer alleen informeren/vragen terugstellen aan de CEO
(ambigue of strategische verzoeken). Expliciet houden aan bestaande guardrail
("geen memory/policy-writes buiten skill-imports") — delegatie is een nieuwe, afgebakende
bevoegdheid, geen algemene bevoegdheidsuitbreiding.

### 2.4 Statusvragen ondersteunen

Kleine aanvulling: als de CEO in het secretary-kanaal vraagt "wat is de status van X",
moet de Secretary het gekoppelde work item kunnen opzoeken (via de metadata-link uit 2.2)
en de actuele fase/`progress_log` teruggeven — dit hergebruikt bestaande leesfunctionaliteit,
geen nieuwe schrijf-bevoegdheid.

**Tests**: nieuwe testfile `tests/test_secretary_delegation.py` — delegatie-actie maakt
daadwerkelijk een work item aan met correcte link-metadata; statusvraag leest het juiste
work item terug; guardrails (geen delegatie bij ambigue input) blijven gehandhaafd.

**Risico**: laag-middel — puur additief aan `secretary.py`, raakt geen bestaande
company-mode-routing. Wel afhankelijk van een schone `org_work_item_planner`-aanroep vanuit
een niet-company-mode-context (secretary draait buiten de normale dispatcher-tick) —
dat integratiepunt moet expliciet getest worden.

---

## Fase 3 — Team chat

### 3.1 Backend: "user" als geldige recipient

`AgentMessage.to_agents` (`opc/core/models.py:1018`) en de fan-out in
`CommunicationManager.rehydrate_queues`/`broadcast`
(`opc/layer2_organization/communication.py:131-144, 2206-2211`) ondersteunen al
multi-recipient tussen rollen. Uitbreiden zodat `"user"` (of een vast CEO-pseudo-agent-id)
een geldig recipient-type is, met aflevering naar een chat-kanaal in plaats van een
agent-inbox.

### 3.2 Kanaal: `activity:{project_id}` promoveren tot team chat

`chat_store.ensure_activity_channel` (`opc/plugins/office_ui/chat_store.py:1316-1329`) is
al projectbreed en al een vast UI-navigatie-item. Voorstel: dit kanaal laten fungeren als
team chat in plaats van alleen een technisch vangnet:
1. Rollen mogen er zelf berichten in posten (niet alleen error/escalation-mirrors) — nieuwe
   entrypoint in `ws_handler.py` analoog aan `_mirror_escalation`, maar voor vrije
   teamberichten (bv. bij het opleveren van een work item, of een expliciete "vraag aan de
   CEO").
2. CEO kan in dit kanaal @-een rol aanspreken; dat bericht wordt via 3.1 naar de
   `AgentMessage`-queue van die rol gerouteerd (niet alleen zichtbaar, ook daadwerkelijk
   bezorgd).
3. Onderscheid bewaren tussen "ruis" (huidige technische mirrors) en "team chat"-berichten
   in de UI (bv. een `kind`-veld op het bericht), zodat het kanaal leesbaar blijft.

### 3.3 UI

`SessionSidebar.tsx` heeft al een vast "Activity"-nav-item; als 3.2 het kanaal semantisch
verbreedt, hernoemen naar "Team" (of een apart nieuw nav-item toevoegen naast Activity, als
we de technische mirror-feed apart willen houden — af te wegen bij implementatie).
`MessageComposer.tsx` hergebruiken zoals bij session-kanalen; enige toevoeging is
@-rol-autocomplete voor het aanspreken van een specifieke rol.

### 3.4 `MeetingRoom` als optie voor gestructureerde team-overleggen

Voor multi-round overleg (niet losse pings) kan `MeetingRoom`
(`opc/core/models.py:1060-1080`, `communication.py` `create_meeting`/`respond_to_meeting`)
op termijn herbruikt worden — heeft al transcript + multi-participant. Voorstel: dit als
losse follow-up behandelen ná 3.1-3.3, niet in de eerste iteratie, omdat het een aparte
UI-representatie (thread-per-meeting) vraagt.

**Tests**: nieuwe testfile `tests/test_team_chat.py` — bericht van CEO in team-kanaal komt
aan in de `AgentMessage`-queue van de aangesproken rol; bericht van een rol verschijnt live
in het kanaal (WS-broadcast); ruis-mirrors blijven onderscheidbaar van team-berichten.

**Risico**: middel — nieuwe UI-flow, maar bouwt op bestaande, al geteste primitieven
(kanaal-model, fan-out-queue). Geen wijziging aan bestaande escalation-/approval-routing.

---

## Volgorde en afhankelijkheden

```
1.1 (sweep)  ──┐
1.2 (metadata) ─┼── onafhankelijk van elkaar, kunnen parallel/in willekeurige volgorde
               │
2.x (secretary delegatie) ── onafhankelijk van Fase 1, kan parallel starten
               │
3.1 (backend fan-out naar user) ── licht afhankelijk van niets in Fase 1/2
3.2-3.3 (team chat kanaal/UI) ── afhankelijk van 3.1
3.4 (MeetingRoom) ── na 3.1-3.3, aparte follow-up

1.3 (identity refactor) ── apart, laatst, eigen review-ronde (hoogste regressierisico)
```

Aanbevolen volgorde qua impact-op-klacht: **1.1 → 2.1-2.4 → 3.1-3.3 → 1.2 → 1.3 → 3.4**
(sweep lost het acute "hangt zonder feedback" op; secretary-delegatie en team chat pakken
de twee expliciet gevraagde features aan; 1.2/1.3 zijn opruim-werk zonder directe
gebruikersklacht; 3.4 is een verrijking, geen basisbehoefte).

## Openstaande beslissingen (input van de gebruiker nodig bij start van elke fase)

- Fase 3.3: apart "Team"-nav-item naast "Activity", of "Activity" volledig ombouwen tot team
  chat? (voorkeur in dit plan: apart houden, af te wegen bij implementatie.)
- Fase 2.1: default-gedrag als de secretary geen `target_role` kan bepalen — altijd naar
  `delivery_lead` (auto-route), of expliciet aan de CEO terugvragen?

Dit document is een plan; er zijn nog geen code-wijzigingen gedaan. Volgende stap is
per fase een losse implementatie-sessie (te beginnen met 1.1, tenzij de gebruiker anders
prioriteert).
