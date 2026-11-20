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
- **D2 — llama-server (Tab Linux).** Implementata (§11): Qwen3.8-27B 8 bit (default) / 4 bit con lo
  script `~/Llama/run-qwen3.8-27b.sh`, Stop con `killall`. Mai acceso insieme a Strata: il Run
  dell'uno con l'altro acceso mostra un dialog "spegni prima …" (§11.3). Gemma più avanti.
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
      - id: llama-server        # dettagli in §11
        label: llama-server
        stop_before_run: true
        default_variant: q8     # radio preselezionato
        variants:
          q8: { label: "8 bit · Qwen3.8-27B UD-Q8_K_XL", command: "cd ~/Llama && ./run-qwen3.8-27b.sh -port 8000 -host 192.168.1.200" }
          q4: { label: "4 bit · …", command: "… -4bit" }
        stop: killall -q -w llama-server || true
        ready: { url: "http://192.168.1.200:8000/health", process: llama-server, timeout: 600 }
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
| GET | `/api/sections/{tab}/{section}/conflicts` | — | sezioni di `exclusive_with` accese o con un job in corso (§11.3) |
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
- [ ] Tab Mac visibile e vuota ("da configurare").
- [ ] `POST` con variante non in config → 400; nessun modo di far eseguire un comando arbitrario.

---

## 10. Fuori scope ora (sessione successiva)

- Contenuto della Tab Mac (D3). Gemma in llama-server (§11).
- Indicatore di stato per sezione (via `strata launch --status --output json`, `/health` sulla porta 8000).
- Log in streaming (SSE) invece del polling.

---

## 11. Sezione llama-server (Tab Linux)

**Run** apre un dialog con due voci di Qwen3.8-27B: **8 bit** (default, già selezionata) e **4 bit**.
**Stop** ferma llama-server. Il job di Run finisce quando il server risponde, non quando lo script
esce. Gemma verrà aggiunto più avanti.

### 11.1 Stato sul PC (20/11/2026)

| Cosa | Stato |
|---|---|
| Script | `~/Llama/run-qwen3.8-27b.sh -port 8000 -host 192.168.1.200 [-4bit]`, sistemato e provato da te (repo Llama, `bbb57db`). Default UD-Q8_K_XL con MTP nel gguf; `-4bit` → UD-Q4_K_XL + drafter `mtp-Qwen3.8-27B-Q4_0.gguf`. Avvia `llama-server` in background con `&` ed **esce subito**; log in `./llama-server-<timestamp>.log` (directory corrente) |
| Stop | `killall llama-server`: nessuno script |
| Gemma | Rimandato (gli `start-Gemma-*.sh` puntano a una cartella che non esiste) |
| Rete | llama-server e Strata ascoltano **entrambi** su `192.168.1.200:8000`. Su raspy `socat-fwd@ubuntuhost` inoltra `127.0.0.1:8080 → 192.168.1.200:8000` e `tailscale serve` espone `:8443 → 127.0.0.1:8080`: l'endpoint della tailnet serve quello dei due che è acceso |
| GPU | Entrambi usano le due Arc: llama-server e Strata non possono girare insieme (porta e GPU) |

### 11.2 Come funziona nell'app

**Run** (un solo job, passi in ordine):

1. `killall -q -w llama-server || true` (`stop_before_run`): aspetta che un llama-server già acceso
   sia uscito, così le GPU sono libere e passare da 8 a 4 bit è un clic.
2. **Porta 8000 libera?** Se è ancora occupata (Strata), il job fallisce subito mostrando il processo
   che la tiene, e lo script non viene lanciato.
3. `cd ~/Llama && ./run-qwen3.8-27b.sh -port 8000 -host 192.168.1.200 [-4bit]`. Il `cd` tiene i log in
   `~/Llama` (via ssh la directory corrente sarebbe `~`).
4. **Attesa**: ogni 2 s llama-server deve essere vivo (`pgrep -x`), deve ascoltare lui sulla 8000
   (`ss -p`: non basta che qualcuno risponda) e `/health` deve dare 200 → `succeeded` ("Pronto").
   Se il processo muore → `failed` subito; oltre 600 s → timeout.

**Stop**: `killall -q -w llama-server || true`. È idempotente: va a buon fine anche se non c'era nulla.

Codice (generico, riusabile per Gemma e per il Mac):

- `config.py`: una variante è una stringa (solo il comando) o `{command, label}`. Nuovi campi di sezione:
  `default_variant` e `ready {url, process, timeout}`.
- `jobs.py`: da `ready` genera i passi 2 e 4 (`port_free_command`, `wait_ready_command`). Nell'output
  compaiono con un'etichetta (`[attendo che llama-server risponda su …]`) invece dello script.
- `app.js`: radio con le label; la variante di default è preselezionata e Avvia è subito attivo.
  Strata è invariato: nessuna preselezione.
- Test: 63, compresi quelli che eseguono gli script generati in bash con `ss`/`pgrep`/`curl` finti.
- Prova sul PC con un server finto (`python3 -m http.server`):
  - "Pronto" quando il processo giusto ascolta;
  - "porta già in uso" con il nome del processo;
  - "non è più in esecuzione" quando il processo manca;
  - un processo lanciato con `&` (come fa lo script) sopravvive alla chiusura della sessione ssh dell'app.

### 11.3 Strata e llama-server: mai accesi insieme (L2, decisa 20/11)

Se uno dei due è acceso, il Run dell'altro **non parte**. Al suo posto un dialog dice di spegnerlo
prima, ad esempio "**Strata è acceso** · llama-server e Strata non possono essere accesi insieme. Spegni
prima Strata con il pulsante Stop della sua card, poi riprova Run". Con OK il dialog si chiude, la card
da spegnere si evidenzia e il focus va sul suo Stop. Nessuno stop automatico.

- **Config**: ogni sezione ha `running` (comando remoto: exit 0 = acceso, 1 = spento, altro = non
  verificabile) e `exclusive_with` (le sezioni incompatibili).
  - llama-server: `pgrep -x llama-server`.
  - Strata: `out=$(~/.local/bin/strata launch --status) || exit 2; … | grep -qE ': (starting|ready) at '`.
    `strata launch --status` stampa una riga `<nome>: <stato> at <url>` per ogni lancio in stato
    `starting` o `ready`, quindi copre anche il caricamento; con nulla acceso stampa "Nothing from
    `strata launch` is running.". Se `strata` stesso fallisce → exit 2, Run rifiutato.
    - *Prima versione (`83a75db`) con `--output json | jq`, sbagliata*: con un lancio attivo Strata
      stampa quella riga anche **dopo** il JSON, jq fallisce (exit 5) e il Run di llama-server con
      Strata acceso dava "non riesco a verificare" invece del dialog. Successo il 20/11 alle 10:34
      dalla console. Corretta con l'output testuale, come suggerito da te.
  - Tempi via ssh: circa 0,4 s per Strata e 0,3 s per llama-server.
- **Quando si controlla**:
  - **al clic su Run**, con `GET /api/sections/{tab}/{section}/conflicts`, prima di aprire il dialog
    delle varianti;
  - **di nuovo nel `POST …/run`**, sotto un lock: due Run incrociati non passano entrambi.
- **Esiti**:
  - acceso, oppure un job in corso sull'altra sezione ("Operazione in corso su …") → `409` con
    `conflicts`, e la pagina mostra il dialog;
  - controllo non riuscito (exit diverso da 0 e 1, o timeout di 20 s) → `502` "Non riesco a verificare
    se … è acceso" e il Run non parte.
- **Stop** non è mai bloccato.
- **Dry-run**: tutti gli engine risultano spenti.
- **Cache**: tutte le risposte hanno `Cache-Control: no-cache`. Senza, dopo un deploy il browser poteva
  riusare un `style.css` o un `app.js` vecchio (osservato nella preview).

### 11.4 Fasi

- [x] **L-A — Script sul PC**: tuo (`bbb57db`).
- [x] **L-B — App**: §11.2, test, dry-run (dialog e sequenza dei passi verificati nel browser).
- [x] **L-C — Deploy** di `f447c00` con `./deploy.sh` (20/11 09:56). Run 8 bit provato dalla console
      (sotto); gli altri criteri restano da provare.
- [x] **L-D — Strata e llama-server esclusivi** (§11.3): app, test, dialog verificato nella preview.
      Deploy di `83a75db` (20/11).

### 11.5 Criteri di accettazione

- [x] Run → 8 bit (preselezionato) → Avvia: `succeeded` solo dopo "Pronto"; il server resta acceso
      dopo il job; `https://ai.exocomet-boga.ts.net:8443/v1/models` risponde dalla tailnet.
      *20/11 09:57, dall'API della console su raspy:*
      - job `succeeded` in 11 s (modello caricato in 9,6 s, gguf in cache);
      - log in `~/Llama/llama-server-20261120-095715.log`;
      - llama-server ancora attivo dopo il job e in ascolto sulla 8000;
      - dalla tailnet `/v1/models` risponde e `/v1/chat/completions` genera a ~50 tok/s.
- [ ] Run → 4 bit mentre gira 8 bit: killall e avvio nello stesso job, poi Q4_K_XL in servizio.
- [x] Run di llama-server con Strata acceso: dialog "Strata is on", nessun job, Strata intatto.
      *20/11, Strata q4 acceso, controllo corretto (output testuale) in produzione:*
      - `GET …/llama-server/conflicts` → Strata `on` in 0,3 s;
      - `POST …/llama-server/run` → `409` "Stop Strata before starting llama-server" e nessun job;
      - nella pagina reale (dal Mac, attraverso il socat della LAN) il dialog "Strata is on", e OK
        evidenzia la card Strata;
      - Strata è rimasto `ready`.
- [x] Run di Strata con llama-server acceso: dialog "llama-server is on" (allora in italiano), nessun job.
      *20/11, con `83a75db` in produzione:*
      - `GET …/strata/conflicts` → llama-server `on` in 0,2 s;
      - `POST …/strata/run` → `409` e nessun job creato;
      - nella pagina reale (tunnel ssh verso raspy:80) Run di Strata mostra il dialog e OK evidenzia la
        card llama-server;
      - llama-server è rimasto acceso con lo stesso PID e Strata spento.
- [x] Stop: nessun llama-server sul PC; un secondo Stop è comunque `succeeded`.
      *20/11 dal log della console*:
      - Stop alle 10:32:59 `succeeded`, poi il controllo dice llama-server spento;
      - Stop alle 10:39:41, con nulla acceso, `succeeded`.
- [ ] Un avvio che fallisce (es. GPU occupate) → `failed` appena il processo muore, non dopo 10 minuti.

## 12. Accesso dalla LAN di casa e interfaccia in inglese (20/11/2026)

**LAN.** Dalla tailnet la console è su `https://ai.exocomet-boga.ts.net`, ma in casa solo raspy e il
laptop sono sulla tailnet. Per gli altri c'è un forwarder socat su raspy, configurato nel progetto:

- **Indirizzo**: `container/lan.env` contiene `LAN_ADDRESS=192.168.1.40` (wlan0, da DHCP),
  `LAN_PORT=80` e `APP_ADDRESS=127.0.0.1:80`. Dalla LAN la console è su **http://192.168.1.40/**.
- **Unità**: `container/llm-console-lan.service` è un'unità utente con
  `socat TCP4-LISTEN:80,bind=192.168.1.40,fork,reuseaddr TCP4:127.0.0.1:80`.
- **Avvio e arresto con la web app**:
  - la Quadlet ha `Wants=llm-console-lan.service`, e l'unità socat ha `BindsTo=` e `After=llm-console.service`;
  - `Restart=always` con `StartLimitIntervalSec=0` riprova finché all'avvio l'indirizzo Wi-Fi non c'è.
- **`install.sh`**:
  - rifiuta un `LAN_ADDRESS` che raspy non ha, o una porta occupata;
  - installa l'unità in `~/.config/systemd/user`;
  - controlla `/healthz` anche sull'indirizzo della LAN.
- **Se il DHCP cambia l'indirizzo di raspy**: aggiornare `lan.env` e rifare `./deploy.sh`.
  Meglio una prenotazione DHCP sul router.
- **Sicurezza**: nessuna autenticazione, come sulla tailnet; i POST richiedono `X-LLM-Console: 1`. Il
  firewall di raspy accetta tutto in ingresso.

**Inglese.** Tutto ciò che la pagina mostra è in inglese:
- testi, dialog e stati della pagina;
- i messaggi dell'API;
- l'output dei job generato dall'app;
- l'orario in formato `en-GB`.

Anche i commenti nel codice sono in inglese. README e PLAN restano in italiano.

- [x] Dal Mac (LAN, non sulla tailnet): `http://192.168.1.40/` risponde (`/healthz`, `/api/config`, pagina).
- [x] Il forwarder è attivo dopo il deploy, si ferma con `systemctl --user stop llm-console` e riparte
      con la web app, anche dopo un restart.
      - Primo tentativo: allo stop finiva `failed`, perché socat esce con 143 su SIGTERM.
      - Corretto con `SuccessExitStatus=143`: ora finisce `inactive`.
- [ ] Al prossimo reboot di raspy: il forwarder riparte da solo, anche se il Wi-Fi arriva dopo la web app.

## 13. Modelli nella pagina, Gemma, messaggi di stato (20/11/2026)

**Messaggi di stato.** Al posto di "Run iq4 running…" la card dice cosa succede e con quale modello:

| | Run | Stop |
|---|---|---|
| in corso | "Strata is launching Qwen3.8-Flash-Next UD-IQ4_XS…" | "Stopping Strata…" |
| riuscito | "Qwen3.8-Flash-Next UD-IQ4_XS is ready to serve requests" | "Strata stopped" |
| fallito | "Strata could not launch … (exit N)" | "Stopping Strata failed (exit N)" |
| timeout | "Launching … timed out" | "Stopping Strata timed out" |

"Ready to serve requests" significa che `/v1/models` ha risposto:
- **llama-server**: il controllo `ready` ora usa `/v1/models` al posto di `/health`;
- **Strata**: nuovo `ready` senza `process`. `strata launch --no-tui` aspetta già il modello, quindi
  questo passo conferma solo che l'API risponde (timeout 120 s). Non c'è controllo della porta prima
  del Run, perché lo gestisce Strata.

**Modelli.** Nuovi campi di config:
- `description`: una riga sotto il titolo della card e nel dialog;
- `variants_legend`: la legenda dei radio;
- `model` per ogni variante.

| Sezione | Descrizione | Varianti (radio) |
|---|---|---|
| Strata | "Model: Qwen3.8-Flash-Next" | `iq4 · UD-IQ4_XS`, `q4 · UD-Q4_K_XL`, `q5 · UD-Q5_K_XL`, `q6 · UD-Q6_K_XL`, `q8 · Q8_0` |
| llama-server | "Models: Qwen3.8-27B, Gemma-4-31B-it" (legenda "Model") | Qwen3.8-27B 8 bit (default) / 4 bit; Gemma-4-31B-it 8 bit (UD-Q8_K_XL) / 4 bit (QAT UD-Q4_K_XL) |

Gemma usa `~/Llama/run-Gemma-4-31b.sh -port 8000 -host 192.168.1.200 [-qat4bit]`. Gli id delle
varianti di llama-server sono ora `qwen-q8`, `qwen-q4`, `gemma-q8` e `gemma-q4`.

**Script sul PC: problemi trovati (20/11, sola lettura, non modificati)**
- `~/Llama/run-Gemma-4-31b.sh` (`239186b`):
  - **percorsi sbagliati**: le righe 62, 80, 132 e 196 usano `/nvme/gguf/gemma-4-31b/…`, ma la
    cartella è `/nvme/gguf/gemma-4-31B` (ext4 distingue maiuscole e minuscole), quindi llama-server
    non trova il modello e il Run fallisce;
  - `#!/bin/bash` è alla riga 18 invece che alla 1. Funziona lo stesso perché viene lanciato da bash,
    ma è fragile.
- `~/Strata/run-unsloth-ud-q6_k_xl.sh` punta a `models/unsloth-UD-Q6_K_XL`, che non esiste (c'è
  `models/UD-Q6_K_XL`).
- `~/Strata/run-unsloth-ud-q8_k_xl.sh` punta a `models/unsloth-UD-Q8_K_XL`, che non esiste (c'è solo
  `models/Q8_0`). L'etichetta `q8 · Q8_0` presume che lo script venga corretto verso `Q8_0`.

**Versione nel footer.** Dopo il deploy di `ce7e711` il footer diceva ancora `d78085b-dirty`. Con il
contenuto identico alla build precedente, podman riusava in cache il layer `ENV` / `LABEL`, anche se
`--build-arg` era cambiato. Ora `build.sh` scrive `container/VERSION` (ignorato da git),
l'immagine lo copia in `/app/VERSION` e l'app lo legge.

- [ ] Deploy, poi Run di ogni voce dalla console: il messaggio passa da "launching" a "ready to serve
      requests". Gemma solo dopo la correzione dei percorsi.
- [x] Il footer mostra la versione giusta anche quando il codice non cambia.
      *20/11, deploy di `ad81ee0`*:
      - il footer e `/app/VERSION` dicono `ad81ee0`;
      - una build di prova con lo stesso contenuto e `LLM_CONSOLE_VERSION=vtest-same-content` ha
        prodotto un'immagine con quella versione (immagine poi rimossa, servizio non toccato).
      - In produzione la pagina mostra le descrizioni dei modelli, e `/api/config` le quattro voci di
        llama-server e le cinque di Strata.

