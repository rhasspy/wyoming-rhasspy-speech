import asyncio
import argparse
import logging
from collections.abc import Iterable
from logging.handlers import QueueHandler
import time
import io
import tarfile
import tempfile
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import List, Optional
from urllib.request import urlopen

import rhasspy_speech
from rhasspy_speech.g2p import LexiconDatabase, guess_pronunciations, get_sounds_like
from flask import Response, jsonify, request, Flask, render_template, redirect, url_for
from yaml import safe_dump, safe_load, SafeDumper

from .models import MODELS
from .hass_api import get_exposed_dict
from .shared import AppState

_DIR = Path(__file__).parent
_LOGGER = logging.getLogger(__name__)


DOWNLOAD_CHUNK_SIZE = 1024 * 10


def get_app(state: AppState) -> Flask:
    app = Flask(
        "rhasspy_speech",
        template_folder=str(_DIR / "templates"),
        static_folder=str(_DIR / "static"),
    )

    @app.route("/")
    def index():
        downloaded_models = {
            m.id for m in MODELS.values() if (state.settings.models_dir / m.id).is_dir()
        }
        return render_template(
            "index.html",
            available_models=MODELS,
            downloaded_models=downloaded_models,
        )

    @app.route("/manage")
    def manage():
        model_id = request.args["id"]
        sentences_path = state.settings.train_dir / model_id / "sentences.yaml"
        return render_template(
            "manage.html", model_id=model_id, has_sentences=sentences_path.exists()
        )

    @app.route("/download")
    def download():
        model_id = request.args["id"]
        return render_template("download.html", model_id=model_id)

    @app.route("/api/download", methods=["POST"])
    def api_download() -> Response:
        model_id = request.args["id"]

        def download_model() -> Iterable[str]:
            try:
                model = MODELS.get(model_id)
                assert model is not None, f"Unknown model: {model_id}"
                with urlopen(
                    model.url
                ) as model_response, tempfile.TemporaryDirectory() as temp_dir:
                    total_bytes: Optional[int] = None
                    content_length = model_response.getheader("Content-Length")
                    if content_length:
                        total_bytes = int(content_length)
                        yield f"Expecting {total_bytes} byte(s)\n"

                    last_report_time = time.monotonic()
                    model_path = Path(temp_dir) / "model.tar.gz"
                    bytes_downloaded = 0
                    with open(model_path, "wb") as model_file:
                        chunk = model_response.read(DOWNLOAD_CHUNK_SIZE)
                        while chunk:
                            model_file.write(chunk)
                            bytes_downloaded += len(chunk)
                            current_time = time.monotonic()
                            if (current_time - last_report_time) > 1:
                                if (total_bytes is not None) and (total_bytes > 0):
                                    yield f"{int((bytes_downloaded / total_bytes) * 100)}%\n"
                                else:
                                    yield f"Bytes downloaded: {bytes_downloaded}\n"
                                last_report_time = current_time
                            chunk = model_response.read(DOWNLOAD_CHUNK_SIZE)

                    yield "Download complete\n"
                    state.settings.models_dir.mkdir(parents=True, exist_ok=True)
                    with tarfile.open(model_path, "r:gz") as model_tar_file:
                        model_tar_file.extractall(state.settings.models_dir)
                    yield "Model extracted\n"
                    yield "Return to models page to continue\n"
            except Exception as err:
                yield f"ERROR: {err}"

        return Response(download_model(), content_type="text/plain")

    @app.route("/api/train", methods=["POST"])
    async def api_train() -> Response:
        model_id = request.args["id"]

        async with state.transcribers_lock:
            state.transcribers.pop(model_id, None)

        def do_training():
            logger = logging.getLogger("rhasspy_speech")
            logger.setLevel(logging.DEBUG)
            log_queue = Queue()
            handler = QueueHandler(log_queue)
            logger.addHandler(handler)

            try:
                yield "Training started\n"
                train_thread = Thread(
                    target=train_model,
                    args=(state, model_id, log_queue),
                    daemon=True,
                )
                train_thread.start()
                while True:
                    log_item = log_queue.get()
                    if log_item is None:
                        break

                    yield log_item.getMessage() + "\n"
                yield "Training complete\n"
            except Exception as err:
                yield f"ERROR: {err}"
            finally:
                logger.removeHandler(handler)

        return Response(do_training(), content_type="text/plain")

    @app.route("/sentences", methods=["GET", "POST"])
    def sentences():
        model_id = request.args["id"]
        sentences = ""
        sentences_path = state.settings.train_dir / model_id / "sentences.yaml"

        if request.method == "POST":
            sentences = request.form["sentences"]
            try:
                with io.StringIO(sentences) as sentences_file:
                    sentences_dict = safe_load(sentences_file)
                    assert "sentences" in sentences_dict, "Missing sentences block"
                    assert sentences_dict["sentences"], "No sentences"

                # Success
                sentences_path.parent.mkdir(parents=True, exist_ok=True)
                sentences_path.write_text(sentences, encoding="utf-8")

                state.skip_words[model_id] = sentences_dict.get("skip_words", [])

                return redirect(url_for("manage", id=model_id))
            except Exception as err:
                return render_template(
                    "sentences.html",
                    model_id=model_id,
                    sentences=sentences,
                    error=err,
                )

        elif sentences_path.exists():
            sentences = sentences_path.read_text(encoding="utf-8")

        return render_template("sentences.html", model_id=model_id, sentences=sentences)

    @app.route("/api/hass_exposed", methods=["POST"])
    async def api_hass_exposed() -> str:
        if state.settings.hass_token is None:
            return "No Home Assistant token"

        exposed_dict = await get_exposed_dict(
            state.settings.hass_token,
            host=state.settings.hass_host,
            port=state.settings.hass_port,
            protocol=state.settings.hass_protocol,
        )
        SafeDumper.ignore_aliases = lambda *args: True
        with io.StringIO() as hass_exposed_file:
            safe_dump({"lists": exposed_dict}, hass_exposed_file, sort_keys=False)
            return hass_exposed_file.getvalue()

    @app.route("/words", methods=["GET", "POST"])
    def words():
        model_id = request.args["id"]
        found = ""
        guessed = ""

        if request.method == "POST":
            words = request.form["words"].split()
            lexicon = LexiconDatabase(
                state.settings.models_dir / model_id / "lexicon.db"
            )

            missing_words = set()
            for word in words:
                if "[" in word:
                    word_prons = get_sounds_like([word], lexicon)
                else:
                    word_prons = lexicon.lookup(word)

                if word_prons:
                    for word_pron in word_prons:
                        phonemes = " ".join(word_pron)
                        found += f'{word}: "/{phonemes}/"\n'
                else:
                    missing_words.add(word)

            if missing_words:
                for word, phonemes in guess_pronunciations(
                    missing_words,
                    state.settings.models_dir / model_id / "g2p.fst",
                    state.settings.tools_dir / "phonetisaurus",
                ):
                    guessed += f'{word}: "/{phonemes}/"\n'

        return render_template(
            "words.html", model_id=model_id, found=found, guessed=guessed
        )

    @app.errorhandler(Exception)
    async def handle_error(err):
        """Return error as text."""
        return (f"{err.__class__.__name__}: {err}", 500)

    return app


# -----------------------------------------------------------------------------


def train_model(state: AppState, model_id: str, log_queue: Queue):
    try:
        _LOGGER.info("Training")
        start_time = time.monotonic()
        sentences_path = state.settings.train_dir / model_id / "sentences.yaml"
        state.settings.train_dir.mkdir(parents=True, exist_ok=True)

        language = model_id.split("-")[0].split("_")[0]
        rhasspy_speech.train_model(
            language=language,
            sentence_files=[sentences_path],
            kaldi_dir=state.settings.tools_dir / "kaldi",
            model_dir=state.settings.models_dir / model_id,
            train_dir=state.settings.train_dir / model_id,
            phonetisaurus_bin=state.settings.tools_dir / "phonetisaurus",
            opengrm_dir=state.settings.tools_dir / "opengrm",
            openfst_dir=state.settings.tools_dir / "openfst",
        )
        _LOGGER.debug(
            "Training completed in %s second(s)", time.monotonic() - start_time
        )
    finally:
        log_queue.put(None)
