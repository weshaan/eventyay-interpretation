import asyncio
import json
import logging
import requests
import threading
import time

from asgiref.sync import sync_to_async
from django.contrib import messages
from django.db import close_old_connections
from django.http import Http404, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView, TemplateView, View
from eventyay.control.permissions import EventPermissionRequiredMixin
from eventyay.control.views.event import EventSettingsViewMixin

from .forms import (
    CONNECT_POST_KEY,
    DISCONNECT_POST_KEY,
    InterpretationSettingsForm,
    TEST_POST_KEY,
)
from .models import RoomInterpretation
from .room_control import serialize_room_interpretation
from .services import caption_payload_for_language
from .settings import (
    get_auth_token,
    get_base_url,
    get_susi_email,
    get_susi_name,
    is_interpretation_enabled,
    is_susi_configured,
    is_susi_connected,
    INTERPRETER_NONE,
    INTERPRETER_SUSI,
)
from .susi import SusiClient, SusiError, susi_host
from .utils import room_settings_url

PLUGIN_MODULE = "interpretation"

logger = logging.getLogger(__name__)

# Seconds between emits while bridging SUSI's caption stream to the browser SSE.
CAPTION_POLL_INTERVAL = 0.5
CAPTION_STREAM_MAX_SECONDS = 600
CAPTION_UPSTREAM_READ_TIMEOUT = 30


class InterpretationEnabledMixin:
    def dispatch(self, request, *args, **kwargs):
        if PLUGIN_MODULE not in request.event.get_plugins():
            return redirect(
                "eventyay_common:event.plugins",
                organizer=request.event.organizer.slug,
                event=request.event.slug,
            )
        return super().dispatch(request, *args, **kwargs)


class InterpretationDashboard(
    InterpretationEnabledMixin,
    EventSettingsViewMixin,
    EventPermissionRequiredMixin,
    FormView,
):
    """Interpretation overview and SUSI connection settings for organizers."""

    form_class = InterpretationSettingsForm
    template_name = "interpretation/dashboard.html"
    permission = "can_change_event_settings"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["obj"] = self.request.event
        kwargs["prefix"] = "interpretation"
        return kwargs

    def get_success_url(self):
        return reverse(
            "plugins:interpretation:dashboard",
            kwargs={
                "organizer": self.request.event.organizer.slug,
                "event": self.request.event.slug,
            },
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        event = self.request.event
        ctx["event"] = event
        ctx["interpretation_enabled"] = is_interpretation_enabled(event)
        ctx["susi_configured"] = is_susi_connected(event)
        ctx["susi_ready"] = is_susi_configured(event)
        ctx["susi_server_host"] = _susi_host(get_base_url(event))
        ctx["susi_account"] = _susi_account_label(event)
        ctx["susi_welcome_name"] = _susi_welcome_name(event)
        ctx["interpretation_providers"] = [
            {"id": INTERPRETER_NONE, "label": _("None")},
            {"id": INTERPRETER_SUSI, "label": _("SUSI Translator")},
        ]
        ctx["selected_provider"] = INTERPRETER_NONE
        if not ctx["susi_configured"]:
            form = ctx.get("form")
            if form and form.errors:
                ctx["selected_provider"] = INTERPRETER_SUSI
            elif self.request.POST.get("interpretation_provider") == INTERPRETER_SUSI:
                ctx["selected_provider"] = INTERPRETER_SUSI
        return ctx

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if DISCONNECT_POST_KEY in request.POST:
            form.run_disconnect_action(request)
            return redirect(self.get_success_url())
        if CONNECT_POST_KEY in request.POST:
            if form.is_valid():
                if form.has_changed():
                    form.save()
                form.run_connect_action(request)
            else:
                return self.form_invalid(form)
            return redirect(self.get_success_url())
        if TEST_POST_KEY in request.POST:
            if form.is_valid():
                if form.has_changed():
                    form.save()
                form.run_test_action(request)
            else:
                return self.form_invalid(form)
            return redirect(self.get_success_url())
        if form.is_valid():
            form.save()
            messages.success(request, _("Your changes have been saved."))
            return redirect(self.get_success_url())
        messages.error(
            request,
            _("We could not save your changes. See below for details."),
        )
        return self.form_invalid(form)


def _susi_welcome_name(event) -> str:
    name = get_susi_name(event)
    email = get_susi_email(event)
    return name or email or ""


def _susi_account_label(event) -> str:
    name = get_susi_name(event)
    email = get_susi_email(event)
    if name and email:
        return f"{name} ({email})"
    return email or name


def _susi_host(base_url: str) -> str:
    if not base_url:
        return ""
    from urllib.parse import urlparse

    return urlparse(base_url).netloc or base_url


class InterpretationRoomList(
    InterpretationEnabledMixin, EventPermissionRequiredMixin, TemplateView
):
    """List the event's rooms with their interpretation status."""

    template_name = "interpretation/rooms.html"
    permission = "can_change_event_settings"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        event = self.request.event
        existing = {
            ri.room_id: ri
            for ri in RoomInterpretation.objects.filter(room__event=event)
        }
        rooms = []
        for room in event.rooms.filter(deleted=False):
            interpretation = existing.get(room.pk)
            data = serialize_room_interpretation(room, event, interpretation)
            rooms.append(
                {
                    "room": room,
                    "status": data["status"],
                    "caption_languages": data["target_languages"],
                    "room_settings_url": room_settings_url(
                        event.organizer.slug, event.slug, room.pk
                    ),
                }
            )
        ctx["event"] = event
        ctx["interpretation_ready"] = is_susi_configured(event)
        ctx["rooms"] = rooms
        return ctx


class InterpretationRoomCaptions(View):
    """Relay SUSI captions to the browser as a same-origin SSE stream."""

    http_method_names = ["get"]

    async def get(self, request, *args, **kwargs):
        pk = kwargs["pk"]

        @sync_to_async
        def load():
            if PLUGIN_MODULE not in request.event.get_plugins():
                return None, "disabled"
            room = get_object_or_404(request.event.rooms.filter(deleted=False), pk=pk)
            interp = RoomInterpretation.objects.filter(room=room).first()
            if interp is None or not interp.susi_session_id:
                return None, "nosession"
            return {
                "base_url": get_base_url(request.event),
                "auth_token": get_auth_token(request.event),
                "tenant_id": interp.susi_session_id,
                "target_languages": list(interp.target_languages or []),
            }, None

        info, err = await load()
        if err == "disabled":
            raise Http404("Interpretation is not enabled for this event.")
        if err == "nosession":
            raise Http404("No running interpretation session for this room.")

        target_lang = request.GET.get("lang", "")
        if (
            target_lang
            and info["target_languages"]
            and target_lang not in info["target_languages"]
        ):
            raise Http404("Unknown caption language for this room.")

        client = SusiClient(info["base_url"], info["auth_token"])
        tenant_id = info["tenant_id"]
        event_slug = request.event.slug
        room_pk = pk
        susi_base = info["base_url"]
        want_tts = request.GET.get("tts") == "1"
        tts_voice = request.GET.get("voice", "").strip() if want_tts else ""

        logger.info(
            "Caption SSE client connected event=%s room=%s tenant_id=%s "
            "target_lang=%s tts=%s susi_host=%s",
            event_slug,
            room_pk,
            tenant_id,
            target_lang or "(source)",
            request.GET.get("tts") == "1",
            susi_host(susi_base),
        )

        if want_tts and not target_lang:
            raise Http404("TTS requires a caption language.")

        # ponytail: long-lived SSE must not hold a request-scoped PG slot (max_connections).
        await sync_to_async(close_old_connections, thread_sensitive=True)()

        try:
            last_chunk_id = int(request.GET.get("last_chunk_id", 0) or 0)
        except (TypeError, ValueError):
            last_chunk_id = 0

        def consume(state):
            chunk_id = last_chunk_id
            read_timeout = None if want_tts else CAPTION_UPSTREAM_READ_TIMEOUT
            while not state["done"]:
                try:
                    upstream = client.open_translate_stream(
                        tenant_id,
                        target_lang=target_lang,
                        last_chunk_id=chunk_id,
                        audio=want_tts,
                        voice=tts_voice,
                        read_timeout=read_timeout,
                    )
                except SusiError as exc:
                    logger.warning(
                        "Caption upstream SSE unavailable event=%s room=%s "
                        "tenant_id=%s tts=%s: %s; using transcript poll fallback",
                        event_slug,
                        room_pk,
                        tenant_id,
                        want_tts,
                        exc,
                    )
                    state["done"] = True
                    return
                events = 0
                try:
                    for raw in upstream.iter_lines(decode_unicode=True):
                        if state["done"]:
                            break
                        if not raw or not raw.startswith("data:"):
                            continue
                        try:
                            data = json.loads(raw.removeprefix("data:").strip())
                        except ValueError:
                            continue
                        if not isinstance(data, dict):
                            continue
                        if data.get("status") == "connected":
                            if isinstance(data.get("tts_voices"), list):
                                with state["lock"]:
                                    state["events"].append(data)
                            continue
                        with state["lock"]:
                            state["events"].append(data)
                            state["latest"] = data
                        events += 1
                        try:
                            cid = int(data.get("chunk_id", 0))
                        except (TypeError, ValueError):
                            cid = 0
                        if cid > chunk_id:
                            chunk_id = cid
                except requests.RequestException as exc:
                    logger.warning(
                        "Caption upstream SSE read error event=%s room=%s tenant_id=%s tts=%s: %s",
                        event_slug,
                        room_pk,
                        tenant_id,
                        want_tts,
                        exc,
                    )
                finally:
                    upstream.close()
                    logger.info(
                        "Caption upstream SSE closed event=%s room=%s tenant_id=%s "
                        "tts=%s events_received=%s",
                        event_slug,
                        room_pk,
                        tenant_id,
                        want_tts,
                        events,
                    )
                if state["done"] or not want_tts:
                    break
                time.sleep(0.5)
            state["done"] = True

        async def event_stream():
            await sync_to_async(close_old_connections, thread_sensitive=True)()
            yield 'data: {"status": "connected"}\n\n'
            state = {
                "latest": None,
                "events": [],
                "lock": threading.Lock(),
                "done": False,
            }
            threading.Thread(target=consume, args=(state,), daemon=True).start()
            poll = sync_to_async(client.latest_transcript, thread_sensitive=False)
            target_requested = bool(target_lang)
            seen_translation = False
            last_forward_key = None
            last_capabilities_key = None
            forwarded = 0
            loops = int(CAPTION_STREAM_MAX_SECONDS / CAPTION_POLL_INTERVAL)

            def forward_data(data):
                nonlocal seen_translation, last_forward_key, last_capabilities_key, forwarded
                if data.get("status") == "connected":
                    serialized = json.dumps(data)
                    if serialized == last_capabilities_key:
                        return ": keepalive\n\n"
                    last_capabilities_key = serialized
                    return f"data: {serialized}\n\n"
                if data.get("translation"):
                    seen_translation = True
                payload = caption_payload_for_language(
                    data, target_requested, seen_translation
                )
                audio_b64 = data.get("audio_b64") if want_tts else None
                if audio_b64:
                    if payload:
                        payload = dict(payload)
                        payload["audio_b64"] = audio_b64
                    else:
                        payload = {
                            "chunk_id": data.get("chunk_id"),
                            "transcript": data.get("transcript") or "",
                            "translation": data.get("translation") or "",
                            "audio_b64": audio_b64,
                        }
                if not payload:
                    return None
                serialized = json.dumps(payload)
                forward_key = (
                    serialized,
                    payload.get("chunk_id"),
                    payload.get("audio_b64"),
                )
                if forward_key == last_forward_key:
                    return ": keepalive\n\n"
                last_forward_key = forward_key
                forwarded += 1
                return f"data: {serialized}\n\n"

            try:
                for _i in range(loops):
                    with state["lock"]:
                        pending = state["events"]
                        state["events"] = []
                    if pending:
                        for data in pending:
                            out = forward_data(data)
                            if out:
                                yield out
                        continue
                    data = state["latest"]
                    if data is None:
                        try:
                            result = await poll(tenant_id)
                            data = result.data or None
                        except SusiError:
                            data = None
                    if data:
                        out = forward_data(data)
                        yield out or ": keepalive\n\n"
                    else:
                        yield ": keepalive\n\n"
                    await asyncio.sleep(CAPTION_POLL_INTERVAL)
            finally:
                state["done"] = True
                await sync_to_async(close_old_connections, thread_sensitive=True)()
                logger.info(
                    "Caption SSE client disconnected event=%s room=%s tenant_id=%s "
                    "tts=%s captions_forwarded=%s",
                    event_slug,
                    room_pk,
                    tenant_id,
                    want_tts,
                    forwarded,
                )

        response = StreamingHttpResponse(
            event_stream(), content_type="text/event-stream"
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response
