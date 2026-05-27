"""TTS engine registry — maps display names to engine classes."""

from .base import TtsEngine

_registry:     dict[str, type[TtsEngine]] = {}   # display_name → class
_ENGINE_KEY:   dict[str, str]              = {}   # display_name → config key
_ENGINE_CLASS: dict[str, type[TtsEngine]] = {}   # config key   → class
_NAME_FOR_KEY: dict[str, str]              = {}   # config key   → display_name


def _register(cls: type[TtsEngine]) -> None:
    name = cls.name
    _registry[name] = cls
    key = getattr(cls, "key", None) or name.lower().replace(" ", "_")
    _ENGINE_KEY[name]  = key
    _ENGINE_CLASS[key] = cls
    _NAME_FOR_KEY[key] = name


def _load_plugin_engines() -> None:
    import importlib.util
    import pathlib
    plugin_dir = pathlib.Path(__file__).parent.parent.parent / "plugins" / "tts"
    if not plugin_dir.exists():
        return
    for py_file in sorted(plugin_dir.glob("*.py")):
        try:
            spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for attr in vars(mod).values():
                if (isinstance(attr, type)
                        and issubclass(attr, TtsEngine)
                        and attr is not TtsEngine
                        and attr.name != "Unknown"):
                    _register(attr)
        except Exception as exc:
            print(f"[TTS plugin] failed to load {py_file.name}: {exc}", flush=True)


_load_plugin_engines()


def engine_names() -> list[str]:
    return list(_registry.keys())


def engine_class(name: str) -> type[TtsEngine]:
    return _registry.get(name) or (next(iter(_registry.values())) if _registry else TtsEngine)


def engine_key(display_name: str) -> str:
    return _ENGINE_KEY.get(display_name, display_name.lower().replace(" ", "_"))


def engine_display_name(key: str) -> str:
    return _NAME_FOR_KEY.get(key, key)


def make_engine(key: str, cfg: dict | None = None) -> TtsEngine:
    """Construct an engine by its config key and restore its config dict."""
    cls = _ENGINE_CLASS.get(key)
    if cls is None:
        cls = _registry.get(key)
    if cls is None and _registry:
        cls = next(iter(_registry.values()))
    if cls is None:
        cls = TtsEngine
    eng = cls()
    if cfg:
        eng.set_config(cfg)
    return eng


# Backward-compat: snapshot of all registered engines (including startup plugins).
ENGINE_NAMES: list[str] = engine_names()
