"""Generate the public versioned schemas from their runtime validators."""
from pathlib import Path
import json

from eviforge.automation import write_schemas
from eviforge.dag.models import GraphSpec, OUTPUT_ADAPTER


def main():
    directory = Path(__file__).resolve().parents[1] / "eviforge" / "schemas"
    write_schemas(directory)
    for name, schema in (("dag-graph-v1.schema.json", GraphSpec.model_json_schema()),
                         ("dag-output-v1.schema.json", OUTPUT_ADAPTER.json_schema())):
        (directory / name).write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
