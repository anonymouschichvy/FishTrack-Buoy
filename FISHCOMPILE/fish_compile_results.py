import json
from pathlib import Path
from collections import defaultdict

# Paths (Best Practice)
DETECT_PATH = Path("..") / "FISHDETECTION" / "data" / "fish_detect_results.json"
ANALYSIS_PATH = Path("..") / "FISHSPECIES" / "data" / "fish_analysis_results.json"
OUTPUT_PATH = Path("data") / "compiled_fish_results.json"

def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def safe_delete(path: Path):
    try:
        if path.exists():
            path.unlink()
            print(f"[CLEANUP] Deleted: {path}")
    except Exception as e:
        print(f"[WARN] Failed to delete {path}: {e}")

def main():
    # ---- LOAD INPUT FILES ----
    detect_data = load_json(DETECT_PATH)
    analysis_data = load_json(ANALYSIS_PATH)

    # ---- STEP 1: Count fish detections per image ----
    detection_counter = defaultdict(lambda: {
        "count": 0,
        "timestamp": None
    })

    for det in detect_data.get("detections", []):
        img = det["Image Name"]
        detection_counter[img]["count"] += 1
        detection_counter[img]["timestamp"] = det.get("Timestamp")

    # ---- STEP 2: Index species analysis by image name ----
    species_map = {}
    for entry in analysis_data:
        img_name = entry["Image Name"]
        species_list = []
        for sp in entry.get("Species Detected", []):
            # Ensure direct and retrieval scores exist
            species_list.append({
                "species": sp.get("species"),
                "confidence": sp.get("confidence"),
                "direct_score": sp.get("direct_score", 0.0),
                "retrieval_score": sp.get("retrieval_score", 0.0)
            })
        species_map[img_name] = species_list

    # ---- STEP 3: Merge everything ----
    merged_results = []
    for image_name, info in detection_counter.items():
        merged_results.append({
            "Image Name": image_name,
            "Timestamp": info["timestamp"],
            "Fish Detected": info["count"],
            "Species Detected": species_map.get(image_name, [])
        })

    # ---- STEP 4: Save output ----
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(merged_results, f, indent=2)

    print(f"[OK] Merged results saved to: {OUTPUT_PATH}")

    # ---- STEP 5: DELETE INPUT FILES AFTER SUCCESS ----
    safe_delete(DETECT_PATH)
    safe_delete(ANALYSIS_PATH)

if __name__ == "__main__":
    main()