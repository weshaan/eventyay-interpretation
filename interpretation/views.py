import asyncio
import json

from asgiref.sync import sync_to_async
from django.contrib import messages
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView, TemplateView, View
from eventyay.control.permissions import EventPermissionRequiredMixin
from eventyay.control.views.event import EventSettingsViewMixin

from .forms import RoomInterpretationForm
from .models import RoomInterpretation
from .services import start_stream_session
from .settings import (
    get_auth_token,
    get_base_url,
    get_susi_client,
    get_susi_email,
    get_susi_name,
    is_interpretation_enabled,
    is_susi_configured,
    is_susi_connected,
)
from .susi import SusiClient, SusiError
from .utils import (
    clear_module_interpretation,
    get_room_stream_url,
    set_module_interpretation,
)

PLUGIN_MODULE = "interpretation"

# Seconds between polls of SUSI's transcript endpoint when bridging it to SSE.
CAPTION_POLL_INTERVAL = 1.5
# Max lifetime of a single SSE connection; the browser EventSource reconnects.
CAPTION_STREAM_MAX_SECONDS = 600


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
    TemplateView,
):
    """Read-only overview of interpretation status for event organizers."""

    template_name = "interpretation/dashboard.html"
    permission = "can_change_event_settings"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        event = self.request.event
        ctx["event"] = event
        ctx["plugin_enabled"] = PLUGIN_MODULE in event.get_plugins()
        ctx["interpretation_enabled"] = is_interpretation_enabled(event)
        ctx["susi_configured"] = is_susi_connected(event)
        ctx["susi_ready"] = is_susi_configured(event)
        ctx["susi_server_host"] = _susi_host(get_base_url(event))
        ctx["susi_account"] = _susi_account_label(event)
        return ctx


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


class _RoomControlBase(InterpretationEnabledMixin, EventPermissionRequiredMixin):
    """Shared helpers for per-room interpretation control views."""

    permission = "can_change_event_settings"

    def get_room(self, pk):
        return get_object_or_404(self.request.event.rooms, pk=pk)

    def rooms_url(self):
        return reverse(
            "plugins:interpretation:rooms",
            kwargs={
                "organizer": self.request.event.organizer.slug,
                "event": self.request.event.slug,
            },
        )


class InterpretationRoomList(_RoomControlBase, TemplateView):
    """List the event's rooms with their interpretation status."""

    template_name = "interpretation/rooms.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        event = self.request.event
        existing = {
            ri.room_id: ri
            for ri in RoomInterpretation.objects.filter(room__event=event)
        }
        rooms = []
        for room in event.rooms.all():
            interpretation = existing.get(room.pk)
            rooms.append(
                {
                    "room": room,
                    "interpretation": interpretation,
                    "stream_url": (
                        interpretation.stream_url
                        if interpretation and interpretation.stream_url
                        else get_room_stream_url(room)
                    ),
                    "status": (
                        interpretation.status
                        if interpretation
                        else RoomInterpretation.STATUS_IDLE
                    ),
                }
            )
        ctx["event"] = event
        ctx["interpretation_ready"] = is_susi_configured(event)
        ctx["rooms"] = rooms
        return ctx


class InterpretationRoomConfig(_RoomControlBase, FormView):
    """Edit interpretation configuration for a single room."""

    template_name = "interpretation/room_config.html"
    form_class = RoomInterpretationForm

    def get_object(self):
        room = self.get_room(self.kwargs["pk"])
        interpretation = RoomInterpretation.objects.filter(room=room).first()
        if interpretation is None:
            interpretation = RoomInterpretation(
                room=room,
                stream_url=get_room_stream_url(room),
            )
        self.room = room
        return interpretation

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.get_object()
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["event"] = self.request.event
        ctx["room"] = getattr(self, "room", None) or self.get_room(self.kwargs["pk"])
        ctx["detected_stream_url"] = get_room_stream_url(ctx["room"])
        return ctx

    def get_success_url(self):
        return self.rooms_url()

    def form_valid(self, form):
        if not is_susi_configured(self.request.event):
            messages.error(
                self.request,
                _("Connect and enable SUSI in video admin before setting up rooms."),
            )
            return redirect(self.get_success_url())
        interpretation = form.save(commit=False)
        interpretation.room = self.get_room(self.kwargs["pk"])
        interpretation.save()
        messages.success(self.request, _("Room interpretation settings saved."))
        return redirect(self.get_success_url())


class InterpretationRoomStart(_RoomControlBase, View):
    """Start a SUSI transcription session for a room's stream."""

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        room = self.get_room(kwargs["pk"])
        event = request.event
        if not is_susi_configured(event):
            messages.error(
                request,
                _("Connect and enable SUSI in video admin before starting a room."),
            )
            return redirect(self.rooms_url())

        interpretation, _created = RoomInterpretation.objects.get_or_create(room=room)

        stream_url = interpretation.stream_url or get_room_stream_url(room)
        if not stream_url:
            messages.error(
                request,
                _("No stream URL is configured for this room."),
            )
            return redirect(self.rooms_url())

        client = get_susi_client(event)
        try:
            tenant_id = start_stream_session(
                client,
                stream_url,
                transcription_provider=interpretation.transcription_provider,
                translation_provider=interpretation.translation_provider,
            )
        except SusiError as exc:
            interpretation.status = RoomInterpretation.STATUS_ERROR
            interpretation.stream_url = stream_url
            interpretation.save()
            messages.error(
                request,
                _("Could not start interpretation: %(error)s") % {"error": str(exc)},
            )
            return redirect(self.rooms_url())

        interpretation.susi_session_id = tenant_id
        interpretation.stream_url = stream_url
        interpretation.status = RoomInterpretation.STATUS_RUNNING
        interpretation.save()

        captions_url = request.build_absolute_uri(
            reverse(
                "plugins:interpretation:room.captions",
                kwargs={
                    "organizer": event.organizer.slug,
                    "event": event.slug,
                    "pk": room.pk,
                },
            )
        )
        if set_module_interpretation(
            room,
            {
                "enabled": True,
                "languages": interpretation.target_languages,
                "url": captions_url,
            },
        ):
            room.save(update_fields=["module_config"])

        if hasattr(interpretation, "log_action"):
            interpretation.log_action(
                "interpretation.room.started",
                data={"tenant_id": tenant_id, "stream_url": stream_url},
            )
        messages.success(
            request,
            _("Interpretation started for room %(room)s.") % {"room": room.name},
        )
        return redirect(self.rooms_url())


class InterpretationRoomStop(_RoomControlBase, View):
    """Stop a room's running SUSI session."""

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        room = self.get_room(kwargs["pk"])
        interpretation = RoomInterpretation.objects.filter(room=room).first()
        if interpretation is None or not interpretation.susi_session_id:
            messages.warning(
                request, _("No running interpretation session for this room.")
            )
            return redirect(self.rooms_url())

        client = get_susi_client(request.event)
        try:
            client.stop_session(interpretation.susi_session_id)
        except SusiError as exc:
            messages.error(
                request,
                _("Could not stop interpretation: %(error)s") % {"error": str(exc)},
            )
            return redirect(self.rooms_url())

        if hasattr(interpretation, "log_action"):
            interpretation.log_action(
                "interpretation.room.stopped",
                data={"tenant_id": interpretation.susi_session_id},
            )
        interpretation.status = RoomInterpretation.STATUS_STOPPED
        interpretation.susi_session_id = ""
        interpretation.save()

        if clear_module_interpretation(room):
            room.save(update_fields=["module_config"])

        messages.success(
            request,
            _("Interpretation stopped for room %(room)s.") % {"room": room.name},
        )
        return redirect(self.rooms_url())


class InterpretationRoomStatus(_RoomControlBase, View):
    """Return the warm-up status of a room's SUSI session as JSON."""

    http_method_names = ["get"]

    def get(self, request, *args, **kwargs):
        room = self.get_room(kwargs["pk"])
        interpretation = RoomInterpretation.objects.filter(room=room).first()
        if interpretation is None:
            raise Http404("No interpretation configured for this room.")

        payload = {
            "status": interpretation.status,
            "session_id": interpretation.susi_session_id,
            "susi": None,
        }
        if interpretation.susi_session_id:
            client = get_susi_client(request.event)
            try:
                result = client.session_status(interpretation.susi_session_id)
                payload["susi"] = result.data.get("status")
            except SusiError as exc:
                payload["susi"] = "error"
                payload["error"] = str(exc)
        return JsonResponse(payload)


class InterpretationRoomTranscript(_RoomControlBase, View):
    """Read-only preview of the latest SUSI transcript for a room (testing aid).

    Proxies the request server-side so the SUSI token is never exposed to the
    browser. Intended for organizers to verify output before the attendee-facing
    caption view exists.
    """

    http_method_names = ["get"]

    def get(self, request, *args, **kwargs):
        room = self.get_room(kwargs["pk"])
        interpretation = RoomInterpretation.objects.filter(room=room).first()
        if interpretation is None or not interpretation.susi_session_id:
            return JsonResponse({"transcript": "", "session": False})

        client = get_susi_client(request.event)
        try:
            result = client.latest_transcript(interpretation.susi_session_id)
        except SusiError as exc:
            return JsonResponse({"transcript": "", "session": True, "error": str(exc)})
        return JsonResponse(
            {
                "transcript": result.data.get("transcript", ""),
                "chunk_id": result.data.get("chunk_id", ""),
                "session": True,
            }
        )


class InterpretationRoomCaptions(View):
    """Relay SUSI captions to the browser as a same-origin SSE stream."""

    http_method_names = ["get"]

    async def get(self, request, *args, **kwargs):
        pk = kwargs["pk"]

        @sync_to_async
        def load():
            if PLUGIN_MODULE not in request.event.get_plugins():
                return None, "disabled"
            room = get_object_or_404(request.event.rooms, pk=pk)
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
        poll = sync_to_async(client.latest_transcript, thread_sensitive=False)

        async def event_stream():
            yield 'data: {"status": "connected"}\n\n'
            last_serialized = None
            loops = int(CAPTION_STREAM_MAX_SECONDS / CAPTION_POLL_INTERVAL)
            for _ in range(loops):
                try:
                    result = await poll(tenant_id)
                except SusiError:
                    yield ": keepalive\n\n"
                    await asyncio.sleep(CAPTION_POLL_INTERVAL)
                    continue

                data = result.data or {}
                transcript = data.get("transcript", "") or ""
                translation = data.get("translation", "") or ""
                if transcript or translation:
                    payload = {
                        "chunk_id": data.get("chunk_id", ""),
                        "transcript": transcript,
                        "translation": translation,
                    }
                    serialized = json.dumps(payload)
                    if serialized != last_serialized:
                        last_serialized = serialized
                        yield f"data: {serialized}\n\n"
                    else:
                        yield ": keepalive\n\n"
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(CAPTION_POLL_INTERVAL)

        response = StreamingHttpResponse(
            event_stream(), content_type="text/event-stream"
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response
