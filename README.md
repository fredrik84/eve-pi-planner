# EVE PI Planner

A multi-character Planetary Industry planner for EVE Online. Plan extraction and factory assignments across an entire fleet to hit a production target with the minimum number of planet interactions.

**Live at [eveindustry.net](https://eveindustry.net)**

---

## What it does

- **Multi-character planning** — distributes extractor and factory planets optimally across all characters in a fleet, respecting per-character planet limits and Command Center Upgrade levels
- **Planet DB** — community-contributed planet resource density data; the planner picks the richest available planets automatically
- **Factory Layout** — generates importable EVE PI templates (P1–P4) sized to the actual planet type and CCU level of each character
- **Fuel Block planning** — plans racial fuel block production with BOM/ME math and market pricing
- **Custom baskets** — admin-defined multi-product production targets run through the same engine
- **Setup Analysis** — compares current colony output against plan demand; shows over/under per material, refill cadence, and skill-ROI advisor
- **Dashboard** — maintenance agenda ("restart extractors / haul P1 / refill factories") with countdown timers
- **Shared plan links** — shareable URLs with Open Graph previews (anonymous by default to protect opsec)

Login is via EVE SSO. No passwords stored; character data is session-scoped and never shared.

---

## Tech stack

| Layer | What |
|---|---|
| Backend | Python 3.12 · FastAPI · psycopg2 |
| Database | PostgreSQL (user data) · SQLite (EVE SDE, read-only) |
| Game data | EVE SDE via Fuzzwork · ESI for live character/wallet data |
| Frontend | Vanilla JS · no framework |
| Deployment | Docker · k3s · ArgoCD GitOps · GitHub Actions |

---

## Development

### Prerequisites

- Python 3.12+
- Docker (for local Postgres) or SQLite fallback
- EVE developer application at [developers.eveonline.com](https://developers.eveonline.com) with scopes:
  ```
  esi-skills.read_skills.v1
  esi-planets.manage_planets.v1
  esi-planets.read_customs_offices.v1
  ```

### Local setup

```bash
git clone https://github.com/fredrik84/eve-pi-planner
cd eve-pi-planner
pip install -r requirements.txt

# Build the SDE database
python scripts/build_sde.py

# Run (uses SQLite for app data by default when no DATABASE_URL set)
uvicorn app.main:app --reload --port 8000
```

Set environment variables (or a `.env` file):

```
EVE_CLIENT_ID=...
EVE_CLIENT_SECRET=...
EVE_CALLBACK_URL=http://localhost:8000/auth/callback
DATABASE_URL=postgresql://user:pass@localhost/evpi  # optional; omit to use SQLite
```

### Tests

```bash
# Planner distribution correctness (requires a running instance with DEBUG_PI=1)
python tests/test_distribution.py --url http://localhost:8000

# Feature flag surface
python tests/test_features.py --url http://localhost:8000
```

---

## Deployment

Production runs via GitOps on the single-node k3s cluster hosted on `server02`. A push to `main`
triggers:

1. **GitHub Actions** builds and pushes `ghcr.io/fredrik84/eve-pi-planner:latest` (~40s)
2. **ArgoCD image updater** detects the new digest and commits a pin to [evpi-gitops](https://github.com/fredrik84/evpi-gitops) (~2 min)
3. **ArgoCD** syncs and rolls the pod (~1 min)

No manual deploy step. The gitops repo holds all cluster manifests.

The local Docker Compose development stack remains available. Because k3s is hosted on the same
node, deployed integration checks can instead run directly with `sudo k3s kubectl`, normally in
the isolated `dev` namespace; use `production` only for read-only diagnostics or deliberate smoke
verification.

---

## Contributing

Planet resource density data is the most valuable community contribution. If you have richness data from in-game scanning (Agency → Resource Harvesting → Planets → Planetary Industry, hover a P0 → Resource Density %), submit it via the **Planet DB → Submit data** tab on the site. Submissions are reviewed by admins before going live.

Want to contribute code? See [CONTRIBUTING.md](CONTRIBUTING.md) for the design philosophy, code style, and PR expectations.

---

## Support

Enjoying the tool? Send ISK in-game to the corporation **Eve PI Planner** [EVPI].

Bugs and feature requests: use the in-app **Report bug** button (top-right header), or open a [GitHub issue](https://github.com/fredrik84/eve-pi-planner/issues).
