"""Create an importable, versioned RAG dataset from a reviewed benchmark file."""

import argparse
import json
from pathlib import Path
from uuid import UUID

from app.modules.knowledge.domain.schemas import EvaluationCreate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-id", type=UUID, required=True)
    parser.add_argument("--split", choices=["development", "held_out"], required=True)
    parser.add_argument(
        "--benchmark",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "dev/fixtures/evaluation/benchmark-v1.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    benchmark = json.loads(args.benchmark.read_text())
    dataset = EvaluationCreate.model_validate(
        {
            "name": f"{benchmark['provenance']} v{benchmark['version']} {args.split}",
            "split": args.split,
            "base_ids": [args.base_id],
            "modes": ["hybrid", "fulltext", "vector"],
            "cases": benchmark[args.split],
        }
    )
    args.output.write_text(dataset.model_dump_json(indent=2) + "\n")
    print(
        f"Saved {len(dataset.cases)} {args.split} cases; "
        f"expert_validated={benchmark['expert_validated']}"
    )


if __name__ == "__main__":
    main()
