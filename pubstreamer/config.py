import configparser
import json
import os

_DEFAULTS = {
    "ui": {
        "language": "",    # "" = system default; "en" = English; "ja" = Japanese
    },
    "server": {
        "base_url": "https://audiopub.site",
        "icecast_host": "live.audiopub.site",
        "icecast_port": "8000",
        "source_user": "source",
        "source_password": "",
        "mountpoint": "",
    },
    "audio": {
        "sample_rate": "48000",
        "channels": "2",
        "chunk_frames": "1024",
        "bitrate": "96",
        "format": "mp3",
        "master_vst_chain": "[]",
        "monitor_device_index": "-1",
        "monitor_gain_db": "0",
    },
    "tts": {
        "enabled": "true",
        "rate": "0",
        "voice_index": "0",
        "max_queue": "5",
    },
    "sources": {
        "list": "[]",
    },
    "mastodon": {
        "instance":      "",
        "token":         "",
        "account_name":  "",
        "post_text":     "",
        "auto_post":     "false",
    },
    "recording": {
        "output_dir":        "",
        "stems":             "false",
        "record_with_stream": "false",
        "format":            "wav",
        "sample_rate":       "48000",
        "bitrate":           "128",
    },
    "audiopub_account": {
        "token":         "",
        "user_id":       "",
        "display_name":  "",
        "stream_key":    "",
        "stream_title":       "",
        "stream_description": "",
        "stream_archive":     "false",
        "stream_resume_mode":  "",    # "auto" | "manual" | ""
        "stream_resume_id":    "",
        "stream_resume_title": "",
    },
}


class Config:
    def __init__(self, path: str = "config.ini"):
        self._path = path
        self._cp = configparser.ConfigParser()
        for section, values in _DEFAULTS.items():
            self._cp[section] = values
        if os.path.exists(path):
            self._cp.read(path)
        else:
            self.save()

    def save(self):
        with open(self._path, "w") as f:
            self._cp.write(f)

    # --- server ---
    @property
    def base_url(self) -> str:
        return self._cp["server"]["base_url"]

    @base_url.setter
    def base_url(self, v: str):
        self._cp["server"]["base_url"] = v

    @property
    def icecast_host(self) -> str:
        return self._cp["server"]["icecast_host"]

    @icecast_host.setter
    def icecast_host(self, v: str):
        self._cp["server"]["icecast_host"] = v

    @property
    def icecast_port(self) -> int:
        return int(self._cp["server"]["icecast_port"])

    @icecast_port.setter
    def icecast_port(self, v: int):
        self._cp["server"]["icecast_port"] = str(v)

    @property
    def source_user(self) -> str:
        return self._cp["server"].get("source_user", "source")

    @source_user.setter
    def source_user(self, v: str):
        self._cp["server"]["source_user"] = v

    @property
    def source_password(self) -> str:
        return self._cp["server"]["source_password"]

    @source_password.setter
    def source_password(self, v: str):
        self._cp["server"]["source_password"] = v

    @property
    def mountpoint(self) -> str:
        return self._cp["server"]["mountpoint"]

    @mountpoint.setter
    def mountpoint(self, v: str):
        self._cp["server"]["mountpoint"] = v

    # --- audio ---
    @property
    def sample_rate(self) -> int:
        return int(self._cp["audio"]["sample_rate"])

    @property
    def channels(self) -> int:
        return int(self._cp["audio"]["channels"])

    @property
    def chunk_frames(self) -> int:
        return int(self._cp["audio"]["chunk_frames"])

    @chunk_frames.setter
    def chunk_frames(self, v: int):
        self._cp["audio"]["chunk_frames"] = str(v)

    @property
    def bitrate(self) -> int:
        return int(self._cp["audio"]["bitrate"])

    @bitrate.setter
    def bitrate(self, v: int):
        self._cp["audio"]["bitrate"] = str(v)

    @property
    def format(self) -> str:
        return self._cp["audio"]["format"]

    @format.setter
    def format(self, v: str):
        self._cp["audio"]["format"] = v

    # --- tts ---
    @property
    def tts_enabled(self) -> bool:
        return self._cp["tts"]["enabled"].lower() == "true"

    @tts_enabled.setter
    def tts_enabled(self, v: bool):
        self._cp["tts"]["enabled"] = "true" if v else "false"

    @property
    def tts_rate(self) -> int:
        return int(self._cp["tts"]["rate"])

    @tts_rate.setter
    def tts_rate(self, v: int):
        self._cp["tts"]["rate"] = str(v)

    @property
    def tts_voice_index(self) -> int:
        return int(self._cp["tts"]["voice_index"])

    @tts_voice_index.setter
    def tts_voice_index(self, v: int):
        self._cp["tts"]["voice_index"] = str(v)

    @property
    def tts_max_queue(self) -> int:
        return int(self._cp["tts"]["max_queue"])

    # --- sources ---
    @property
    def sources(self) -> list:
        return json.loads(self._cp["sources"]["list"])

    @sources.setter
    def sources(self, v: list):
        self._cp["sources"]["list"] = json.dumps(v)

    @property
    def master_vst_chain(self) -> list:
        return json.loads(self._cp["audio"].get("master_vst_chain", "[]"))

    @master_vst_chain.setter
    def master_vst_chain(self, v: list):
        self._cp["audio"]["master_vst_chain"] = json.dumps(v)

    @property
    def monitor_device_index(self) -> "int | None":
        v = int(self._cp["audio"].get("monitor_device_index", "-1"))
        return None if v < 0 else v

    @monitor_device_index.setter
    def monitor_device_index(self, v: "int | None"):
        self._cp["audio"]["monitor_device_index"] = str(-1 if v is None else v)

    # --- mastodon ---
    @property
    def mastodon_instance(self) -> str:
        return self._cp["mastodon"]["instance"]

    @mastodon_instance.setter
    def mastodon_instance(self, v: str):
        self._cp["mastodon"]["instance"] = v

    @property
    def mastodon_token(self) -> str:
        return self._cp["mastodon"]["token"]

    @mastodon_token.setter
    def mastodon_token(self, v: str):
        self._cp["mastodon"]["token"] = v

    @property
    def mastodon_account_name(self) -> str:
        return self._cp["mastodon"].get("account_name", "")

    @mastodon_account_name.setter
    def mastodon_account_name(self, v: str):
        self._cp["mastodon"]["account_name"] = v

    @property
    def mastodon_post_text(self) -> str:
        return self._cp["mastodon"]["post_text"]

    @mastodon_post_text.setter
    def mastodon_post_text(self, v: str):
        self._cp["mastodon"]["post_text"] = v

    @property
    def mastodon_auto_post(self) -> bool:
        return self._cp["mastodon"]["auto_post"].lower() == "true"

    @mastodon_auto_post.setter
    def mastodon_auto_post(self, v: bool):
        self._cp["mastodon"]["auto_post"] = "true" if v else "false"

    # --- audiopub account ---
    @property
    def ap_token(self) -> str:
        return self._cp["audiopub_account"].get("token", "")

    @ap_token.setter
    def ap_token(self, v: str):
        self._cp["audiopub_account"]["token"] = v

    @property
    def ap_user_id(self) -> str:
        return self._cp["audiopub_account"].get("user_id", "")

    @ap_user_id.setter
    def ap_user_id(self, v: str):
        self._cp["audiopub_account"]["user_id"] = v

    @property
    def ap_display_name(self) -> str:
        return self._cp["audiopub_account"].get("display_name", "")

    @ap_display_name.setter
    def ap_display_name(self, v: str):
        self._cp["audiopub_account"]["display_name"] = v

    @property
    def ap_stream_key(self) -> str:
        return self._cp["audiopub_account"].get("stream_key", "")

    @ap_stream_key.setter
    def ap_stream_key(self, v: str):
        self._cp["audiopub_account"]["stream_key"] = v

    @property
    def ap_stream_title(self) -> str:
        return self._cp["audiopub_account"].get("stream_title", "")

    @ap_stream_title.setter
    def ap_stream_title(self, v: str):
        self._cp["audiopub_account"]["stream_title"] = v

    @property
    def ap_stream_description(self) -> str:
        return self._cp["audiopub_account"].get("stream_description", "")

    @ap_stream_description.setter
    def ap_stream_description(self, v: str):
        self._cp["audiopub_account"]["stream_description"] = v

    @property
    def ap_stream_archive(self) -> bool:
        return self._cp["audiopub_account"].get("stream_archive", "false").lower() == "true"

    @ap_stream_archive.setter
    def ap_stream_archive(self, v: bool):
        self._cp["audiopub_account"]["stream_archive"] = "true" if v else "false"

    @property
    def stream_resume_mode(self) -> str:
        return self._cp["audiopub_account"].get("stream_resume_mode", "")

    @stream_resume_mode.setter
    def stream_resume_mode(self, v: str):
        self._cp["audiopub_account"]["stream_resume_mode"] = v

    @property
    def stream_resume_id(self) -> str:
        return self._cp["audiopub_account"].get("stream_resume_id", "")

    @stream_resume_id.setter
    def stream_resume_id(self, v: str):
        self._cp["audiopub_account"]["stream_resume_id"] = v

    @property
    def stream_resume_title(self) -> str:
        return self._cp["audiopub_account"].get("stream_resume_title", "")

    @stream_resume_title.setter
    def stream_resume_title(self, v: str):
        self._cp["audiopub_account"]["stream_resume_title"] = v

    # --- recording ---
    @property
    def rec_output_dir(self) -> str:
        return self._cp["recording"].get("output_dir", "")

    @rec_output_dir.setter
    def rec_output_dir(self, v: str):
        self._cp["recording"]["output_dir"] = v

    @property
    def rec_stems(self) -> bool:
        return self._cp["recording"].get("stems", "false").lower() == "true"

    @rec_stems.setter
    def rec_stems(self, v: bool):
        self._cp["recording"]["stems"] = "true" if v else "false"

    @property
    def rec_with_stream(self) -> bool:
        return self._cp["recording"].get("record_with_stream", "false").lower() == "true"

    @rec_with_stream.setter
    def rec_with_stream(self, v: bool):
        self._cp["recording"]["record_with_stream"] = "true" if v else "false"

    @property
    def rec_format(self) -> str:
        return self._cp["recording"].get("format", "wav")

    @rec_format.setter
    def rec_format(self, v: str):
        self._cp["recording"]["format"] = v

    @property
    def rec_sample_rate(self) -> int:
        return int(self._cp["recording"].get("sample_rate", "48000"))

    @rec_sample_rate.setter
    def rec_sample_rate(self, v: int):
        self._cp["recording"]["sample_rate"] = str(v)

    @property
    def rec_bitrate(self) -> int:
        return int(self._cp["recording"].get("bitrate", "128"))

    @rec_bitrate.setter
    def rec_bitrate(self, v: int):
        self._cp["recording"]["bitrate"] = str(v)

    @property
    def monitor_gain_db(self) -> float:
        return float(self._cp["audio"].get("monitor_gain_db", "0"))

    @monitor_gain_db.setter
    def monitor_gain_db(self, v: float):
        self._cp["audio"]["monitor_gain_db"] = str(v)

    # --- ui ---
    @property
    def language(self) -> str:
        return self._cp["ui"].get("language", "")

    @language.setter
    def language(self, v: str):
        self._cp["ui"]["language"] = v
