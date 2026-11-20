# llm-console

Web app che avvia e ferma le inference engine sugli host remoti (PC Linux, Mac) via ssh.
Gira in un container podman rootless su **raspy**, su `127.0.0.1:80`, ed è raggiungibile:
- dalla tailnet, tramite `tailscale serve`, su `https://ai.exocomet-boga.ts.net`;
- dalla LAN di casa su `http://192.168.1.40/`, tramite un forwarder socat su raspy (`container/lan.env`).
Piano e decisioni: [PLAN.md](PLAN.md).

## Configurazione

[`config.yaml`](config.yaml) contiene host, tab, sezioni e comandi. È l'unica fonte dei
comandi: il browser invia solo gli id di tab, sezione e variante. Una sezione senza
`variants` non ha Run e una senza `stop` non ha Stop: la pagina la mostra con i pulsanti
disabilitati.

Campi opzionali di una sezione:

- **Variante come `{command, label, model}`**: oltre alla forma stringa.
  - `label` è il testo del radio button.
  - `model` è il nome del modello usato nella riga di stato ("Strata is launching *model*…", poi
    "*model* is ready to serve requests").
- **`description`**: una riga sotto il titolo della card e nel dialog di Run, per esempio i modelli serviti.
- **`variants_legend`**: la legenda dei radio button; il default è "Quantization".
- **`default_variant`**: il radio preselezionato.
- **`stop_before_run`**: il Run esegue prima lo `stop`.
- **`ready: {url, process, timeout}`**: il Run è completato solo quando `url` risponde (es.
  `/v1/models`).
  - `process` (facoltativo) serve per uno script che avvia il server in background ed esce subito,
    come `run-qwen3.8-27b.sh`.
  - Con `process`, prima del Run controlla che la porta dell'`url` sia libera.
  - Con `process`, dopo il Run aspetta anche che `process` ascolti su quella porta, e se muore il
    Run fallisce subito.
- **`running`**: comando remoto che dice se l'engine è acceso (exit 0 = acceso, 1 = spento, altro =
  non verificabile).
- **`exclusive_with: [id, …]`**: le sezioni della stessa tab che non possono essere accese insieme a
  questa. Ognuna deve avere `running`. Mentre una di loro è accesa, o ha un job in corso, il Run viene
  rifiutato (`409` con `conflicts`) e la pagina dice di spegnerla prima. La pagina lo chiede già al
  clic su Run, con `GET /api/sections/{tab}/{section}/conflicts`.

## Sviluppo sul Mac

```bash
uv sync
uv run pytest
LLM_CONSOLE_DRY_RUN=1 uv run uvicorn --factory app.main:create_app --port 8765 --reload
```

In dry-run (`LLM_CONSOLE_DRY_RUN=1`) non viene eseguito niente: ogni job mostra il comando
ssh che lancerebbe e dura `LLM_CONSOLE_DRY_RUN_DELAY` secondi (default 3).

Dopo aver cambiato le dipendenze in `pyproject.toml`, rigenera quelle dell'immagine:

```bash
uv export --no-dev --no-hashes --no-header --no-emit-project -o requirements.txt
```

## API

| Metodo | Path | |
|---|---|---|
| GET | `/api/config` | tab e sezioni (senza comandi) |
| POST | `/api/sections/{tab}/{section}/run` | body `{"variant": "q4"}` → 202 `{job_id}`; 409 se c'è già un job o se una sezione di `exclusive_with` è accesa (`conflicts`); 502 se non si riesce a verificarlo |
| GET | `/api/sections/{tab}/{section}/conflicts` | `{conflicts: [{id, label, reason: on/busy}]}`: cosa rifiuterebbe un Run adesso |
| POST | `/api/sections/{tab}/{section}/stop` | → 202 `{job_id}`, 409 se c'è già un job |
| GET | `/api/jobs/{id}` | stato, exit code, ultime 200 righe di output |
| GET | `/api/sections/{tab}/{section}/job` | ultimo job della sezione |
| GET | `/healthz` | `ok` |

I POST richiedono l'header `X-LLM-Console: 1` (protezione CSRF), per esempio:

```bash
curl -X POST -H 'X-LLM-Console: 1' -H 'Content-Type: application/json' \
     -d '{"variant":"q4"}' http://127.0.0.1/api/sections/linux/strata/run
```

## Container e deploy

- `container/build.sh` costruisce l'immagine (`container` su macOS, podman su Linux).
- `container/install.sh`, su raspy:
  - costruisce l'immagine;
  - installa la unit Quadlet `container/llm-console.container` e il forwarder per la LAN
    `container/llm-console-lan.service` (socat, indirizzo in `container/lan.env`);
  - riavvia il servizio. Il forwarder parte e si ferma insieme alla web app.
- `./deploy.sh`, dal Mac, copia il checkout in `raspy:~/llm-console` e lancia `install.sh`.

Prerequisiti su raspy (Fase 0 di PLAN.md): `net.ipv4.ip_unprivileged_port_start=80`,
`~/.ssh/known_hosts` con la chiave del PC, lingering attivo. La regola Tailscale
(`tailscale serve --bg --https=443 http://127.0.0.1:80`) si aggiunge a mano dopo il primo deploy.

Log su raspy: `journalctl --user-unit llm-console -f` (web app) e
`journalctl --user-unit llm-console-lan -f` (forwarder LAN).
