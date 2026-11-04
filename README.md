# isometric-city

A zoomable isometric pixel-art map of a real city, generated tile by tile from 3D city
data with a fine-tuned image model. Inspired by isometric.nyc and tinyrj.com.

See [PLAN.md](PLAN.md) for the full plan.

## Getting started

```bash
cp .env.example .env   # add your API keys
uv sync                # Python pipeline deps
uv run pytest          # geometry tests

# plan the tile grid (+ a PNG preview of the grid over the boundary)
uv run python -m pipeline.plan_tiles --area config/area.yaml \
    --out data/plan/sodermalm.json --preview data/plan/sodermalm.png

# renderer (Node 20+): browse tiles interactively, or batch-render PNGs headless
cd render && npm install && npx playwright install chromium
npm run render -- view --plan ../data/plan/sodermalm.json            # needs GOOGLE_MAPS_API_KEY
npm run render -- render --plan ../data/plan/sodermalm.json \
    --out ../data/renders/sodermalm --block=-1,-1,1,1                 # add --source test: no API
cd ..

# stitch a block and print seam metrics
uv run python -m pipeline.stitch_block --plan data/plan/sodermalm.json \
    --tiles-dir data/renders/sodermalm --out data/stitch/block.png --mark

# tile pyramid + viewer (static site in dist/<map>/)
uv run python -m pipeline.build_pyramid --plan data/plan/sodermalm.json \
    --tiles-dir data/renders/sodermalm --out dist/sodermalm-renders
cd viewer && npm install
npm run viewer -- dev --map ../dist/sodermalm-renders     # local dev server
npm run viewer -- build --map ../dist/sodermalm-renders   # write index.html into the map folder
cd ..

# pixel art from renders with Gemini on Vertex AI (ADC: gcloud auth application-default login)
uv run python -m pipeline.stylize --renders data/renders/sodermalm --tiles=0_0 \
    --prompt style/prompts/<version>.md --out data/styled/<run> --project <gcp-project> \
    --model gemini-3-pro-image [--ref style/references/<img>.png] [--pixel 2]
```

> **Closed mode** (see PLAN.md): Google Photorealistic 3D Tiles may not be stored, published,
> turned into derived art or used for AI training. Everything derived from them stays local
> and private; switch to a licensed source before publishing anything.
