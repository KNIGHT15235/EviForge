"""Read official package registries; do not infer server versions from examples."""
import json
import urllib.request

if __name__ == "__main__":
    with urllib.request.urlopen("https://pypi.org/pypi/serena-agent/json", timeout=30) as response:
        package = json.load(response)
    print(json.dumps({"serena_version": package["info"]["version"], "requires_python": package["info"]["requires_python"]}))
