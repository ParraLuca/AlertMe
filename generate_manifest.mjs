// Scanne runs/*/scored.csv et écrit manifest.json (chemins web en '/')
// Usage: node generate_manifest.mjs
import { promises as fs } from "fs";
import path from "path";

const ROOT = process.cwd();
const RUNS_DIR = path.join(ROOT, "runs");
const OUT = path.join(ROOT, "manifest.json");
const toWebPath = (p) => p.replace(/\\/g, "/");

async function exists(p){ try { await fs.stat(p); return true; } catch { return false; } }

async function main(){
  if (!await exists(RUNS_DIR)) {
    console.error("Dossier 'runs' introuvable à la racine.");
    process.exit(1);
  }
  const entries = await fs.readdir(RUNS_DIR, { withFileTypes: true });
  const sources = [];
  for (const ent of entries){
    if (!ent.isDirectory()) continue;
    const label = ent.name;
    const csvFs = path.join(RUNS_DIR, label, "scored.csv");
    const csvWeb = toWebPath(path.join("runs", label, "scored.csv"));
    if (await exists(csvFs)) sources.push({ label, path: csvWeb });
  }
  sources.sort((a,b)=>a.label.localeCompare(b.label));
  const payload = { generated_at: new Date().toISOString(), sources };
  await fs.writeFile(OUT, JSON.stringify(payload, null, 2), "utf-8");
  console.log(`OK: ${sources.length} source(s) → ${path.relative(ROOT, OUT)}`);
  console.table(sources);
}

main().catch(e=>{ console.error(e); process.exit(1); });
