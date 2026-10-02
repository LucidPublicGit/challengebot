"""Generic announcement composer; no catalog mutation or automatic posting."""

import logging
import secrets
import sqlite3
import time
from io import BytesIO

import discord
from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError

from app.catalog import list_challenges
from app.catalog_changes import change_draft, list_changes, mark_announced
from app.catalog_announcements import IMAGE_DIR, announcement_text, text_chunks
from app.config import get_settings, merge_runtime
from app.db import db_session, get_all_settings
from app.web.app import _esc, _flash_html, _page
from app.services.rewards import resolve_announce_channel

logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 10 * 1024 * 1024
PHYSICAL_IMAGE = IMAGE_DIR / "physical-option-6.png"


def register_announcements(app, auth) -> None:
    # One-use form tokens prevent CSRF and double-submit/reload duplicates.
    # ponytail: process-local tokens; use shared storage if multiple workers are needed.
    tokens: dict[str, tuple[float, tuple[int, ...]]] = {}

    def composer(
        *, title="", message="", image="none", channel_id="", error="", sent="",
        status=200, bot=None, change_ids=(),
    ):
        now = time.monotonic()
        for key in list(tokens):
            if tokens[key][0] <= now:
                del tokens[key]
        if len(tokens) >= 128:
            del tokens[next(iter(tokens))]
        token = secrets.token_urlsafe(32)
        tokens[token] = (now + 1800, tuple(change_ids))
        with db_session() as conn:
            runtime = merge_runtime(get_settings(), get_all_settings(conn))
        options = ['<option value="">Configured announcement channel</option>']
        known = set()
        for guild in getattr(bot, "guilds", []) or []:
            for channel in guild.text_channels:
                value = str(channel.id)
                known.add(value)
                selected = " selected" if value == channel_id else ""
                options.append(
                    f'<option value="{value}"{selected}>'
                    f'{_esc(guild.name)} / #{_esc(channel.name)}</option>'
                )
        if channel_id and channel_id not in known:
            options.append(
                f'<option value="{_esc(channel_id)}" selected>{_esc(channel_id)}</option>'
            )
        images = "".join(
            f'<option value="{value}"{" selected" if image == value else ""}>{label}</option>'
            for value, label in (
                ("none", "No image"), ("physical", "体力チャレンジ — selected Physical banner"),
                ("upload", "Upload another image"),
            )
        )
        pending_count = len(list_changes(pending_only=True))
        tracking_notice = (
            f'<p class="msg">{len(change_ids)} tracked changes are included. '
            'They will be marked announced only after all posts succeed.</p>'
            if change_ids else ""
        )
        body = f"""
        {_flash_html(saved=sent, err=error)}
        <div class="card">
          <div class="row">
            <div><h2>Start from a draft</h2>
              <p class="muted">Use the approved image and current Physical challenge
              details, then edit anything before publishing.</p>
              <a class="btn secondary" href="/announcements?preset=physical">Physical challenges draft</a>
              <a class="btn" href="/announcements?preset=changes">Unannounced changes ({pending_count})</a>
              <a class="btn secondary" href="/announcements/history">Change history</a>
              <a class="btn secondary" href="/announcements">Blank announcement</a>
            </div>
            <img src="/announcements/physical-image" alt="Approved 体力チャレンジ banner"
                 style="max-width:100px;border-radius:6px;flex:0 0 auto"/>
          </div>
        </div>
        <div class="grid">
          <form id="announcement-form" class="card" method="post"
                action="/announcements" enctype="multipart/form-data">
            <h2>Compose</h2>
            {tracking_notice}
            <input type="hidden" name="token" value="{token}"/>
            <label for="announcement-title">Title</label>
            <input id="announcement-title" name="title" value="{_esc(title)}"
                   maxlength="256" required/>
            <label for="announcement-message">Message</label>
            <textarea id="announcement-message" name="message" rows="14"
                      maxlength="12000" required>{_esc(message)}</textarea>
            <p class="muted">Any announcement: new, changed, or retired challenges,
              events, or general news. Discord Markdown is supported.</p>
            <label for="announcement-image">Image</label>
            <select id="announcement-image" name="image">{images}</select>
            <div id="upload-field">
              <label for="announcement-upload">Upload image</label>
              <input id="announcement-upload" type="file" name="upload"
                     accept="image/png,image/jpeg,image/webp,image/gif"/>
              <p class="muted">PNG, JPEG, WebP, or GIF. Maximum 10 MB or the server limit.</p>
            </div>
            <label for="announcement-channel">Destination</label>
            <select id="announcement-channel" name="channel_id">{"".join(options)}</select>
            <p class="muted">Default channel ID: {_esc(runtime.get("announce_channel_id") or "not configured")}.
              No role or everyone pings. Publishing does not change the catalog.</p>
            <div class="check-row">
              <input type="checkbox" id="confirm-publish" name="confirmed" value="yes" required/>
              <label for="confirm-publish">I checked the preview and destination.</label>
            </div>
            <button type="submit">Publish announcement</button>
            <p class="muted">Nothing is sent until you publish. Form expires after 30 minutes.</p>
            <p class="muted">After an error, reselect any uploaded image before trying again.</p>
          </form>
          <section class="card" aria-labelledby="preview-heading">
            <h2 id="preview-heading">Preview — not published</h2>
            <h3 id="preview-title"></h3>
            <img id="preview-image" alt="Announcement image preview"
                 style="width:100%;height:auto;border-radius:6px" hidden/>
            <div id="preview-message" style="white-space:pre-wrap;overflow-wrap:anywhere;margin-top:1rem"></div>
            <p class="muted">Preview shows raw Markdown. Discord formats it when sent;
              long messages are split into multiple posts.</p>
          </section>
        </div>
        <script>
        (() => {{
          const form = document.getElementById('announcement-form');
          const title = document.getElementById('announcement-title');
          const message = document.getElementById('announcement-message');
          const image = document.getElementById('announcement-image');
          const upload = document.getElementById('announcement-upload');
          const preview = document.getElementById('preview-image');
          let objectURL = null;
          function refresh() {{
            document.getElementById('preview-title').textContent = title.value;
            document.getElementById('preview-message').textContent = message.value;
            document.getElementById('upload-field').hidden = image.value !== 'upload';
            upload.disabled = image.value !== 'upload';
            upload.required = image.value === 'upload';
            if (objectURL) {{ URL.revokeObjectURL(objectURL); objectURL = null; }}
            let src = '';
            if (image.value === 'physical') src = '/announcements/physical-image';
            if (image.value === 'upload' && upload.files.length) {{
              objectURL = URL.createObjectURL(upload.files[0]); src = objectURL;
            }}
            preview.hidden = !src;
            if (src) preview.src = src; else preview.removeAttribute('src');
          }}
          form.addEventListener('input', refresh);
          form.addEventListener('change', refresh);
          refresh();
        }})();
        </script>
        """
        response = _page(
            "Announcements", body, "/announcements", status=status,
            subtitle="Compose any announcement, preview it, then publish to Discord.",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/announcements")
    def announcement_page(request: Request, preset: str = "", _: None = Depends(auth)):
        draft = {}
        if preset == "changes":
            message, ids = change_draft()
            physical_added = any(
                c["id"] in ids and c["change"] == "new"
                and c["after"].get("category") == "physical"
                for c in list_changes(pending_only=True)
            )
            draft = {
                "title": "Challenge catalog updates", "message": message,
                "image": "physical" if physical_added else "none", "change_ids": ids,
            }
        elif preset == "physical":
            items = list_challenges()
            year = max((c["year"] for c in items), default=2026)
            physical = [
                c for c in items if c["year"] == year and c.get("category") == "physical"
            ]
            draft = {
                "title": "New Physical challenges",
                "message": announcement_text(physical, "new"),
                "image": "physical",
            }
        return composer(bot=request.app.state.bot, **draft)

    @app.get("/announcements/history")
    def history(_: None = Depends(auth)):
        rows = []
        for event in list_changes(limit=100):
            ch = event["after"] or event["before"]
            snapshots = _esc(
                "Before:\n" + (event["before_json"] or "Not captured / new entry")
                + "\n\nAfter:\n" + (event["after_json"] or "Removed")
            )
            rows.append(
                f"<tr><td>{_esc(event['created_at'])}</td>"
                f"<td>{_esc(event['change'])}</td><td>{_esc(ch['title'])}"
                f"<span class='sub'>{_esc(event['challenge_id'])}</span>"
                f"<details><summary>Definition snapshots</summary>"
                f"<pre style='white-space:pre-wrap;overflow-wrap:anywhere'>{snapshots}</pre>"
                f"<p>{_esc(event['note'])}</p></details></td>"
                f"<td>{_esc(event['announced_at'] or 'Not announced')}</td></tr>"
            )
        body = (
            '<p><a class="btn" href="/announcements?preset=changes">Draft unannounced changes</a></p>'
            '<p class="muted">Latest 100 recorded changes. Removed definitions remain here.</p>'
            '<div class="card" style="overflow:auto"><table><thead><tr>'
            '<th>Recorded (UTC)</th><th>Change</th><th>Challenge</th><th>Announced (UTC)</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'
        )
        return _page("Change history", body, "/announcements")

    @app.get("/announcements/physical-image")
    def physical_image(_: None = Depends(auth)):
        return FileResponse(PHYSICAL_IMAGE, media_type="image/png")

    @app.post("/announcements")
    async def publish(
        request: Request,
        title: str = Form(""),
        message: str = Form(""),
        image: str = Form("none"),
        channel_id: str = Form(""),
        token: str = Form(""),
        confirmed: str = Form(""),
        upload: UploadFile | None = File(None),
        _: None = Depends(auth),
    ):
        expires, change_ids = tokens.pop(token, (0, ()))
        if expires <= time.monotonic():
            raise HTTPException(403, "Form expired or already submitted. Reload Announcements.")
        bot = request.app.state.bot
        title, message, channel_id = title.strip(), message.strip(), channel_id.strip()
        posted = 0
        file = None
        try:
            if not title or len(title) > 256 or not message or len(message) > 12000:
                raise ValueError("Enter a title (1–256 characters) and message (1–12,000 characters).")
            if confirmed != "yes":
                raise ValueError("Check the preview and destination, then confirm publishing.")
            if change_ids and not set(change_ids).issubset(
                {c["id"] for c in list_changes(pending_only=True)}
            ):
                raise ValueError("Some changes were already announced. Reload the unannounced changes draft.")
            if image not in ("none", "physical", "upload"):
                raise ValueError("Choose no image, the Physical banner, or an upload.")
            if channel_id and not channel_id.isdigit():
                raise ValueError("Choose a valid Discord channel.")
            if bot is None or not bot.is_ready():
                raise ValueError("Discord bot is not ready. Wait or start the bot, then try again.")
            target = await resolve_announce_channel(bot, channel_id=channel_id or None)
            if target is None:
                raise ValueError("Announcement channel not found. Check the channel setting and bot access.")
            if image == "upload":
                if upload is None:
                    raise ValueError("Choose an image to upload.")
                data = await upload.read(MAX_IMAGE_BYTES + 1)
                if len(data) > min(MAX_IMAGE_BYTES, target.guild.filesize_limit):
                    raise ValueError("Image exceeds the upload limit. Choose a smaller image.")
                try:
                    with Image.open(BytesIO(data)) as picture:
                        extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif"}.get(picture.format)
                        picture.verify()
                    if not extension:
                        raise ValueError("Use a PNG, JPEG, WebP, or GIF image.")
                except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
                    raise ValueError("Invalid image. Use a PNG, JPEG, WebP, or GIF.") from exc
                file = discord.File(BytesIO(data), filename=f"announcement.{extension}")
            elif image == "physical":
                if PHYSICAL_IMAGE.stat().st_size > target.guild.filesize_limit:
                    raise ValueError("Physical banner exceeds this server's upload limit.")
                file = discord.File(PHYSICAL_IMAGE)
            embed = discord.Embed(title=title, color=0xF5B041)
            kwargs = {"embed": embed, "allowed_mentions": discord.AllowedMentions.none()}
            if file:
                kwargs["file"] = file
                embed.set_image(url=f"attachment://{file.filename}")
            await target.send(**kwargs)
            posted += 1
            for chunk in text_chunks(message):
                await target.send(chunk, allowed_mentions=discord.AllowedMentions.none())
                posted += 1
            if change_ids:
                mark_announced(change_ids)
        except ValueError as exc:
            return composer(
                title=title, message=message, image=image, channel_id=channel_id,
                error=str(exc), status=400, bot=bot, change_ids=change_ids,
            )
        except (discord.HTTPException, OSError, sqlite3.Error):
            logger.exception("web announcement failed")
            return composer(
                title=title, message=message, image=image, channel_id=channel_id, bot=bot,
                change_ids=change_ids,
                error=f"Publishing failed after {posted} message(s). Check Discord before retrying "
                      "to avoid duplicates; check bot permissions and reselect any uploaded image.",
                status=502,
            )
        finally:
            if file:
                file.close()
            if upload:
                await upload.close()
        return composer(
            sent=f"Published to #{target.name}.", bot=bot,
        )
