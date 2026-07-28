import hashlib
import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from change_delivery.domain import LoadedProjectConfig, ProjectConfig
from change_delivery.errors import ConfigurationError


def load_project_config(path: Path) -> LoadedProjectConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        config = ProjectConfig.model_validate(raw)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigurationError(f"项目配置无效：{path}") from exc

    if config.production_enabled:
        raise ConfigurationError("第一版禁止启用正式发布写操作")

    canonical = json.dumps(
        config.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return LoadedProjectConfig(
        config=config,
        config_hash=digest,
        source=path.resolve(),
    )
