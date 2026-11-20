# llm-console

Web app che avvia e ferma le inference engine sugli host remoti (PC Linux, Mac) via ssh.
Gira in un container podman rootless su **raspy**, su `127.0.0.1:80`, ed è raggiungibile
dalla tailnet tramite `tailscale serve` su `https://ai.exocomet-boga.ts.net`.
Piano e decisioni: [PLAN.md](PLAN.md).

## Configurazione

[`config.yaml`](config.yaml) contiene host, tab, sezioni e comandi. È l'unica fonte dei
comandi: il browser invia solo gli id di tab, sezione e variante. Una sezione senza
`variants` non ha Run e una senza `stop` non ha Stop: la pagina la mostra con i pulsanti
disabilitati.

Campi opzionali di una sezione:

- **Variante come `{command, label}`**: oltre alla forma stringa. `label` è il testo del radio button.
- **`default_variant`**: il radio preselezionato.
- **`stop_before_run`**: il Run esegue prima lo `stop`.
- **`ready: {url, process, timeout}`**: per uno script che avvia il server in background ed esce
  subito, come `run-qwen3.8-27b.sh`.
  - Prima del Run controlla che la porta dell'`url` sia libera.
  - Dopo il Run aspetta che `process` ascolti su quella porta e che `url` risponda.
  - Se `process` muore, il Run fallisce subito.

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
| POST | `/api/sections/{tab}/{section}/run` | body `{"variant": "q4"}` → 202 `{job_id}`, 409 se c'è già un job |
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
- `container/install.sh`, su raspy, costruisce l'immagine, installa la unit Quadlet
  `container/llm-console.container` e riavvia il servizio.
- `./deploy.sh`, dal Mac, copia il checkout in `raspy:~/llm-console` e lancia `install.sh`.

Prerequisiti su raspy (Fase 0 di PLAN.md): `net.ipv4.ip_unprivileged_port_start=80`,
`~/.ssh/known_hosts` con la chiave del PC, lingering attivo. La regola Tailscale
(`tailscale serve --bg --https=443 http://127.0.0.1:80`) si aggiunge a mano dopo il primo deploy.

Log: `journalctl --user-unit llm-console -f` su raspy.
