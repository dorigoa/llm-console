# llm-console — Piano

Web App per avviare/fermare le inference engine sugli host remoti (PC Linux e Mac),
in un container podman su **raspy**, raggiungibile dalla tailnet su
`https://ai.exocomet-boga.ts.net`. Nessuna autenticazione: il perimetro di
sicurezza è la tailnet.

---

## 1. Stato verificato (19/11/2026, ispezione in sola lettura)

| Cosa | Trovato | Conseguenza per il piano |
|---|---|---|
| raspy | Debian 13 (trixie), aarch64, podman 5.4.2 **rootless**, netavark/pasta, linger attivo | Stesso pattern di `ai-console`: Quadlet utente + `UserNS=keep-id` |
| Porta 80 su raspy | `net.ipv4.ip_unprivileged_port_start = 1024` | Rootless **non può** aprire la porta 80 → prerequisito sysctl (Fase 0) |
| Tailscale serve | `:443 → http://127.0.0.1:8501` è **già occupata da ai-console** | `tailscale serve --bg --https=443 http://127.0.0.1:80` **sostituirebbe** ai-console → decisione aperta D1 |
| `ai-console/gateway/tailscale-serve.sh` | Fa `tailscale serve reset` e rimette `443 → 8501` | Se rilanciato cancella la regola di llm-console → va aggiornato insieme |
| Container su raspy | Unico attivo: `ai-console` (podman rootless, unit Quadlet **utente** `ai-console.service`, porta 8501). Docker: solo `open-webui`, fermo. Nessuno ascolta sulla 80 | ai-console **non** blocca la 80 (la blocca solo la sysctl) e non serve fermarlo per la 443: la regola `tailscale serve` si sovrascrive comunque. Per fermarlo: `systemctl --user stop ai-console` (non `sudo …`, non `sudo podman`); per non farlo ripartire al boot: spostare `~/.config/containers/systemd/ai-console.container` + `systemctl --user daemon-reload` (le unit Quadlet non si possono `disable`) |
| ai-console oggi | Gestisce già Strata via ssh e fallisce con `strata: command not found` (rc=127) | Conferma il problema del PATH remoto (§5.3) |
| SSH raspy → PC/Mac | Funziona con `id_ecdsa` e `id_ed25519` (utente `alvise`) | Riuso `id_ecdsa` montato read-only, come ai-console |
| known_hosts di raspy | Riga 15 per `192.168.1.200` è **obsoleta** (`SHA256:2Ub+0p…`); il PC oggi presenta `SHA256:hrVqRc+4w3pzMsG0aY8WJo+I0IbIkh86+TpKDYAjFZ0` (verificato leggendo `/etc/ssh/ssh_host_ed25519_key.pub` sul PC) | Con `StrictHostKeyChecking=yes` nel container la connessione fallirebbe → fix in Fase 0 |
| Script `~/Strata/run-unsloth-ud-*.sh` e `stop.sh` | Corretti (commit `dc7c09b`, 19/11 22:25): `#!/bin/bash` + `/home/alvise/.local/bin/strata`; `bash -n` ok | Funzionano via ssh non interattiva anche se `~/.local/bin` non è nel PATH (D4 chiusa) |
| `strata launch --no-tui` | "serve, print the address and return" → il comando ssh **ritorna** quando il server è su | L'esecuzione è un job finito (minuti), non un processo da tenere aperto |
| `strata launch --restart` | Senza `--restart`, se gira già un modello con altre scelte, `--no-tui` **rifiuta** | Cambiare quantizzazione richiede stop → run (vedi §5.3) |
| `strata launch --status --output json` | Disponibile | Base per un indicatore di stato (fuori scope ora, §9) |

---

## 2. Decisioni aperte

- **D1 — Conflitto sulla 443.** → **Decisa (19/11): la 443 va a llm-console**; ai-console è stato
  fermato con `systemctl --user stop ai-console` (ripartirà al boot finché il suo Quadlet resta in
  `~/.config/containers/systemd/`). Opzioni che erano sul tavolo:
  1. llm-console prende la 443, ai-console si sposta su un'altra porta HTTPS (es. `--https=7443`); *(raccomandata se llm-console deve essere l'URL "principale")*
  2. llm-console va su un'altra porta HTTPS (es. `--https=10443`), ai-console resta dov'è;
  3. llm-console sostituisce ai-console (che viene dismessa).
- **D2 — llama-server (Tab Linux).** Comandi di Run/Stop e varianti non ancora definiti → la sezione viene creata ma con pulsanti disabilitati finché non sono in `config.yaml`.
- **D3 — Tab Mac.** Contenuto non ancora definito → tab presente, vuota ("da configurare").
- ~~**D4 — Script Strata.**~~ Chiusa: gli script sul PC hanno shebang e path assoluto. L'app prefissa comunque `PATH="$HOME/.local/bin:$PATH"` (innocuo, utile per i comandi futuri di llama-server/Mac).

---

## 3. Architettura

```
browser (tailnet)
   │ HTTPS
   ▼
ai.exocomet-boga.ts.net:443        tailscale serve su raspy
   │ HTTP
   ▼
127.0.0.1:80  (raspy, pasta) ──► container llm-console, uvicorn su 0.0.0.0:8080
                                   │ ssh -i id_ecdsa alvise@…   (job asincroni)
                    ┌──────────────┴──────────────┐
                    ▼                             ▼
          PC 192.168.1.200 (Ubuntu)      Mac 192.168.1.101 (MacStudio1)
          ~/Strata/run-*.sh, stop.sh     (D3)
          llama-server (D2)
```

**Stack:** Python 3.13 + FastAPI + uvicorn; frontend HTML/CSS/JS statico (nessun build step,
`<dialog>` nativo per la finestra di Run). Immagine `python:3.13-alpine` + `openssh-client` + `tini`,
come ai-console. *(Alternativa: NiceGUI per coerenza con ai-console; scelgo FastAPI perché
l'API resta testabile con pytest e usabile anche da `curl`.)*

---

## 4. Struttura del repository

```
llm-console/
├── PLAN.md
├── README.md
├── pyproject.toml            # fastapi, uvicorn, pydantic, pyyaml; dev: pytest, httpx
├── config.yaml               # host, tab, sezioni, varianti → comandi (unica fonte dei comandi)
├── app/
│   ├── main.py               # FastAPI: route API + static files
│   ├── config.py             # caricamento/validazione config.yaml (pydantic)
│   ├── runner.py             # costruzione comando ssh, esecuzione async, timeout
│   └── jobs.py               # registro job in memoria, lock per sezione
├── static/
│   ├── index.html            # tab Linux / Mac, card per sezione
│   ├── app.js                # render da /api/config, dialog, polling job
│   └── style.css             # Run verde, Stop rosso, responsive
├── container/
│   ├── Containerfile
│   ├── entrypoint.sh         # copia le chiavi in ~/.ssh con i permessi giusti (da ai-console)
│   ├── llm-console.container # unit Quadlet
│   └── install.sh            # build su raspy + install Quadlet + restart + health check
├── deploy.sh                 # dal Mac: rsync su raspy:~/llm-console e lancia container/install.sh
└── tests/
    ├── test_config.py
    ├── test_api.py           # runner finto, nessun ssh reale
    └── test_runner.py        # argv generato, quoting, timeout
```

---

## 5. Backend

### 5.1 `config.yaml`

```yaml
ssh:
  identity: /home/alvise/.ssh/id_ecdsa        # path nel container (copiato da entrypoint.sh)
  known_hosts: /run/host-ssh/known_hosts      # montato read-only
  connect_timeout: 10
  remote_path_prefix: '$HOME/.local/bin'      # D4: prefisso al PATH remoto

hosts:
  pc:  { address: 192.168.1.200, user: alvise }
  mac: { address: 192.168.1.101, user: alvise }

tabs:
  - id: linux
    label: Linux
    host: pc
    sections:
      - id: llama-server
        label: llama-server
        variants: {}            # D2: da definire
        stop: null              # D2
      - id: strata
        label: Strata
        stop_before_run: true   # §5.3
        run_timeout: 900        # secondi
        variants:               # l'ordine è quello dei radio button
          iq4: ~/Strata/run-unsloth-ud-iq4_xs-v31.sh
          q4:  ~/Strata/run-unsloth-ud-q4_k_xl.sh
          q5:  ~/Strata/run-unsloth-ud-q5_k_xl.sh
          q6:  ~/Strata/run-unsloth-ud-q6_k_xl.sh
          q8:  ~/Strata/run-unsloth-ud-q8_k_xl.sh
        stop: ~/Strata/stop.sh
  - id: mac
    label: Mac
    host: mac
    sections: []                # D3
```

Una sezione senza `variants`/`stop` viene mostrata con i pulsanti disabilitati.

### 5.2 API

| Metodo | Path | Body | Risposta |
|---|---|---|---|
| GET | `/api/config` | — | tab, sezioni, varianti (solo id/etichette, flag `enabled`) |
| POST | `/api/sections/{tab}/{section}/run` | `{"variant": "q4"}` | `202 {"job_id": …}`; `409` se un job della sezione è in corso; `400` variante sconosciuta |
| POST | `/api/sections/{tab}/{section}/stop` | — | `202 {"job_id": …}`; `409` se in corso |
| GET | `/api/jobs/{job_id}` | — | `{state: running/succeeded/failed/timeout, exit_code, started_at, ended_at, output_tail}` |
| GET | `/api/sections/{tab}/{section}/job` | — | ultimo job della sezione (per ripristinare la UI dopo un reload) |
| GET | `/healthz` | — | `ok` |

Il client manda **solo identificatori** (`tab`, `section`, `variant`): il comando remoto
viene sempre dalla config, mai dalla richiesta.

### 5.3 Esecuzione

- Comando costruito come lista argv e lanciato con `asyncio.create_subprocess_exec` (nessuna shell locale):
  ```
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30
      -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/run/host-ssh/known_hosts
      -o IdentitiesOnly=yes -i ~/.ssh/id_ecdsa
      alvise@192.168.1.200
      'PATH="$HOME/.local/bin:$PATH" ~/Strata/run-unsloth-ud-q4_k_xl.sh'
  ```
- **Run con `stop_before_run: true`**: nello stesso job esegue prima `stop.sh` (exit code ignorato se
  non c'era nulla in esecuzione), poi lo script della variante. Così passare da q4 a q8 è un clic,
  senza modificare gli script con `--restart`.
- Job in background con timeout (Run 15 min, Stop 2 min, configurabili); stdout+stderr uniti,
  conservati gli ultimi ~200 righe; al timeout il processo ssh viene terminato.
- **Un job alla volta per sezione** (lock), così doppi clic o due utenti non lanciano due avvii.
- Registro job in memoria (si perde al riavvio del container: accettabile).
- Log applicativo su stdout → `journalctl --user-unit llm-console`.

---

## 6. Frontend

- Header "llm-console", due tab: **Linux** e **Mac** (tab attiva ricordata in `localStorage`).
- Tab Linux: due card, **llama-server** e **Strata**. Ogni card: titolo, host (`PC · 192.168.1.200`),
  pulsante **Run** (verde) e **Stop** (rosso).
- **Run** apre un `<dialog>` modale con radio button `iq4 / q4 / q5 / q6 / q8` (nessuna preselezione,
  il pulsante "Avvia" si abilita dopo la scelta) + "Annulla". "Avvia" → `POST …/run`.
- **Stop** → `POST …/stop` direttamente.
- Durante il job: pulsanti della card disabilitati, spinner, polling `GET /api/jobs/{id}` ogni 2 s;
  a fine job esito (✓/✗ + exit code) e output espandibile.
- Al caricamento, per ogni sezione legge l'ultimo job così la UI resta coerente dopo un reload.
- Layout responsive (usabile da telefono sulla tailnet), tema chiaro/scuro da `prefers-color-scheme`.

---

## 7. Container e servizio su raspy

**Containerfile** (base `docker.io/library/python:3.13-alpine`): `openssh-client`, `tini`, utente
`alvise` con UID/GID dell'host, `pip install .`, `ENTRYPOINT ["tini","--","/entrypoint.sh"]`,
`CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8080"]`.

**Quadlet `llm-console.container`** (stesso schema di ai-console):

```ini
[Unit]
Description=llm-console in a podman container

[Container]
Image=localhost/llm-console:latest
ContainerName=llm-console
UserNS=keep-id
PublishPort=127.0.0.1:80:8080
Volume=%h/.ssh/id_ecdsa:/run/host-ssh/nodes/id_ecdsa:ro
Volume=%h/.ssh/id_ecdsa.pub:/run/host-ssh/nodes/id_ecdsa.pub:ro
Volume=%h/.ssh/known_hosts:/run/host-ssh/known_hosts:ro
Volume=%h/llm-console/config.yaml:/app/config.yaml:ro

[Service]
Restart=on-failure
RestartSec=5
SuccessExitStatus=143

[Install]
WantedBy=default.target
```

`config.yaml` montato da fuori: aggiungere llama-server o il Mac (D2/D3) = modificare il file e
`systemctl --user restart llm-console`, senza rebuild.

**Deploy:** `deploy.sh` dal Mac → `rsync` su `raspy:~/llm-console` → `container/install.sh` su raspy
(build `podman build -t localhost/llm-console:latest`, copia Quadlet in
`~/.config/containers/systemd/`, `daemon-reload`, `restart`, health check su `http://127.0.0.1/healthz`).
Build direttamente su raspy (arm64 nativo, niente cross-build).

---

## 8. Fasi

### Fase 0 — Prerequisiti (manuali su raspy/PC, alcuni con `sudo`)

**Fase 0 completata (19/11):** **1 ✓** (raspy: `ip_unprivileged_port_start=80`, persistente in
`/etc/sysctl.d/50-llm-console.conf`; `podman run -p 127.0.0.1:80:…` rootless ok; il PC è tornato a 1024)
· **2 ✓** (known_hosts di raspy ha `SHA256:hrVqRc+…`, ssh con `StrictHostKeyChecking=yes` ok)
· **3 ✓** (D1) · **4 ✓** (`ud-q4_k_xl-256k: ready at http://192.168.1.200:8000`, `/v1/models` → 200 da raspy).

Nota: su raspy `~/.ssh/config` usa `ControlMaster auto` (socket in `~/.ssh/pc/`) e
`StrictHostKeyChecking no`. Una connessione già aperta viene riusata e salta il controllo della
chiave: per i test usare `-o ControlPath=none`. Il container non usa quella config (`entrypoint.sh`
ne scrive una sua), quindi lì il controllo è sempre effettivo.

1. **Porta 80 rootless** (su raspy non c'è il binario `sysctl`):
   ```bash
   echo 'net.ipv4.ip_unprivileged_port_start=80' | sudo tee /etc/sysctl.d/50-llm-console.conf
   echo 80 | sudo tee /proc/sys/net/ipv4/ip_unprivileged_port_start
   ```
2. **known_hosts del PC su raspy** (la chiave attuale `SHA256:hrVqRc+4w3…` coincide con quella letta sul PC):
   ```bash
   ssh-keygen -f ~/.ssh/known_hosts -R 192.168.1.200
   ssh -o ControlPath=none -o StrictHostKeyChecking=accept-new alvise@192.168.1.200 true
   ```
3. **Chiudere D1** (porta HTTPS) e, se ai-console si sposta, aggiornare `ai-console/gateway/tailscale-serve.sh`.
4. **Prova manuale da raspy** che un Run via ssh ritorni e che il server sopravviva alla chiusura di ssh:
   ```bash
   ssh -n alvise@192.168.1.200 'PATH="$HOME/.local/bin:$PATH" ~/Strata/run-unsloth-ud-q4_k_xl.sh'; echo "exit=$?"
   ssh alvise@192.168.1.200 '~/.local/bin/strata launch --status'
   ssh alvise@192.168.1.200 '~/Strata/stop.sh'; echo "exit=$?"
   ssh alvise@192.168.1.200 '~/Strata/stop.sh'; echo "exit=$? (a vuoto)"
   ```
   Se il server muore quando ssh si chiude, il runner userà `setsid nohup … &` e un controllo di stato
   al posto dell'exit code.

### Fase 1 — Backend
`git init`, `pyproject.toml`, `config.py` + `config.yaml`, `runner.py`, `jobs.py`, `main.py`;
test con runner finto (argv corretto, 409 su job concorrente, 400 su variante ignota, timeout).

### Fase 2 — Frontend
`index.html` / `app.js` / `style.css` come in §6; prova in locale sul Mac con `uvicorn --reload`
e un runner "dry-run" (`LLM_CONSOLE_DRY_RUN=1` → stampa il comando invece di eseguirlo).

### Fase 3 — Container
`Containerfile`, `entrypoint.sh` (adattato da ai-console), Quadlet, `install.sh`, `deploy.sh`.

### Fase 4 — Deploy e Tailscale

**Fatto (19/11):** `./deploy.sh` → `llm-console.service` attivo su raspy, in ascolto solo su `127.0.0.1:80`;
`tailscale serve --bg --https=443 http://127.0.0.1:80` applicato. Verificato da raspy via
`https://ai.exocomet-boga.ts.net`: `/healthz` 200, pagina e `/api/config` ok, POST senza header
`X-LLM-Console` → 403, variante ignota / sezione non configurata → 400. Dal container: ssh con
`StrictHostKeyChecking=yes` verso PC (`strata launch --status` ok) e Mac ok.
Run/Stop di Strata provati dall'utente da un laptop sulla tailnet: ok.
ai-console (deprecato) disabilitato: Quadlet spostato in `~/ai-console.container.disabled` su raspy,
al boot parte solo `llm-console.service`. Non rilanciare `ai-console/gateway/tailscale-serve.sh`: il suo
`reset` rimetterebbe la 443 su ai-console. Nota: MacStudio1 non è sulla tailnet.
Prova di reboot (19/11 23:24): `llm-console.service` attivo ~20 s dopo il boot, ai-console assente,
`ip_unprivileged_port_start=80` persistente, `tailscale serve` 443 → `127.0.0.1:80` e `/healthz` 200 via
tailnet, ssh dal container al PC ok, nessuna unit in stato failed.
`./deploy.sh`, poi su raspy (con la porta scelta in D1; serve `sudo` se `alvise` non è operator):
```bash
tailscale serve --bg --https=443 http://127.0.0.1:80
```
e aggiornamento di `ai-console/gateway/tailscale-serve.sh` perché un suo `reset` non cancelli la regola.

### Fase 5 — Verifica end-to-end (§9)

---

## 9. Criteri di accettazione

- [x] `systemctl --user status llm-console` attivo; parte da solo dopo reboot di raspy.
- [ ] `curl http://127.0.0.1/healthz` su raspy → `ok`; `ss -ltn` mostra `127.0.0.1:80` e **non** 0.0.0.0:80.
- [ ] `https://ai.exocomet-boga.ts.net` (o la porta di D1) apre la UI da un dispositivo sulla tailnet; non raggiungibile da fuori tailnet.
- [ ] Tab Linux → Strata → Run → `q4` → Avvia: il job termina `succeeded`, `strata launch --status` sul PC mostra Q4_K_XL, `http://192.168.1.200:8000` risponde.
- [ ] Run → `q8` mentre gira q4: stop + avvio nello stesso job, poi Q8_K_XL in servizio.
- [ ] Stop: job `succeeded`, nessun modello in servizio sul PC.
- [ ] Doppio clic su Run: il secondo riceve 409, nessun doppio avvio.
- [ ] llama-server e Tab Mac visibili con pulsanti disabilitati ("da configurare").
- [ ] `POST` con variante non in config → 400; nessun modo di far eseguire un comando arbitrario.

---

## 10. Fuori scope ora (sessione successiva)

- Comandi di llama-server (D2) e contenuto della Tab Mac (D3).
- Indicatore di stato per sezione (via `strata launch --status --output json`, health HTTP delle porte 8000/8080).
- Log in streaming (SSE) invece del polling.
- Nota: Strata e llama-server sul PC usano le stesse GPU (`--gpu 0,1`); un eventuale vincolo
  "uno alla volta" tra sezioni si valuta quando D2 è definito.
