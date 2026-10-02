"""Achievement administration, using the existing authenticated admin UI."""

import asyncio
import json
import secrets
import time
import uuid
from io import BytesIO

from fastapi import Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from PIL import Image, UnidentifiedImageError

from app.catalog import list_challenges
from app.services import achievements as ach
from app.web.app import _esc, _flash_html, _page

MAX_IMAGE_BYTES = 4 * 1024 * 1024


def register_achievements(app, auth) -> None:
    # ponytail: single-process form tokens; shared storage if deployed with multiple workers.
    tokens = {}

    def token_field():
        now = time.monotonic()
        for token in list(tokens):
            if tokens[token] < now:
                del tokens[token]
        if len(tokens) >= 256:
            del tokens[next(iter(tokens))]
        token = secrets.token_urlsafe(32)
        tokens[token] = now + 1800
        return f'<input type="hidden" name="token" value="{token}"/>'

    def check_token(token):
        if tokens.pop(token, 0) < time.monotonic():
            raise HTTPException(403, "This form has expired. Reload the page and try again.")

    def find(aid):
        item = next((item for item in ach.list_definitions(include_removed=True)
                     if item["id"] == aid), None)
        if item is None:
            raise HTTPException(404, "Achievement not found.")
        return item

    def fields(item=None):
        item = item or {}
        images = ['<option value="">No image</option>']
        ach.IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        for path in sorted([*ach.IMAGE_DIR.glob("*.png"), *ach.upload_dir().glob("*.png")]):
            selected = " selected" if path.name == item.get("image_file") else ""
            images.append(f'<option value="{_esc(path.name)}"{selected}>{_esc(path.name)}</option>')
        rule = json.dumps(json.loads(item.get("rule_json") or '{"kind":"clears","count":1}'), indent=2)
        return f"""
        {token_field()}
        <div class="grid">
          <div class="span-2"><label for="ach-title">Title</label>
            <input id="ach-title" name="title" maxlength="80" value="{_esc(item.get('title') or '')}" required spellcheck="true"/></div>
          <div class="span-2"><label for="ach-description">Requirement</label>
            <textarea id="ach-description" name="description" maxlength="500" required spellcheck="true">{_esc(item.get('description') or '')}</textarea></div>
          <div><label for="ach-position">Catalog order</label>
            <input id="ach-position" name="position" type="number" min="0" max="100000" value="{int(item.get('position') or 0)}"/></div>
          <div><label for="ach-enabled">Visibility</label>
            <select id="ach-enabled" name="enabled">
              <option value="1" {"selected" if item.get('enabled', 1) else ""}>Published</option>
              <option value="0" {"selected" if not item.get('enabled', 1) else ""}>Removed from cards and lists</option>
            </select></div>
          <div class="span-2"><label for="ach-rule">Unlock rule (JSON)</label>
            <textarea id="ach-rule" name="rule_json" rows="7" required spellcheck="false">{_esc(rule)}</textarea>
            <p class="muted">Distinct clears: <code>{{"kind":"clears","count":5}}</code><br/>
            Media formats: <code>{{"kind":"formats","count":4}}</code><br/>
            One completed challenge from each group:
            <code>{{"kind":"groups","groups":[["easy-1","medium-1"],["easy-2"]]}}</code><br/>
            Changing the rule recalculates eligibility and unlock dates. Titles and images do not affect eligibility.</p>
          </div>
          <div><label for="ach-image">Existing image</label>
            <select id="ach-image" name="image_file">{"".join(images)}</select></div>
          <div><label for="ach-upload">Upload replacement image</label>
            <input id="ach-upload" type="file" name="image" accept="image/png,image/jpeg,image/webp"/>
            <p class="muted">Up to 4 MB and 1024 × 1024 pixels. Saved as a PNG.</p></div>
        </div>
        """

    @app.get("/achievements")
    def achievements_page(saved: str = "", _: None = Depends(auth)):
        rows = []
        for item in ach.list_definitions(include_removed=True):
            rows.append(
                f'<tr><td><a href="/achievements/{_esc(item["id"])}">{_esc(item["title"])}</a>'
                f'<span class="sub mono">{_esc(item["id"])}</span></td>'
                f'<td>{_esc(item["description"])}</td>'
                f'<td>{"Published" if item["enabled"] else "Removed"}</td></tr>'
            )
        challenges = "".join(
            f'<li><code>{_esc(c["id"])}</code> — {_esc(c["title"])}</li>'
            for c in list_challenges()
        )
        body = f"""
        {_flash_html(saved="Saved." if saved else "")}
        <div class="card"><table><thead><tr><th>Achievement</th><th>Requirement</th><th>Visibility</th></tr></thead>
        <tbody>{"".join(rows)}</tbody></table></div>
        <details class="block"><summary>Add an achievement</summary><div class="block-body">
          <p class="muted">A permanent, generic ID is generated automatically.</p>
          <form method="post" action="/achievements" enctype="multipart/form-data">
            {fields()}<button type="submit">Add achievement</button>
          </form>
        </div></details>
        <details class="block"><summary>Challenge IDs for unlock rules</summary>
          <div class="block-body"><ul>{challenges}</ul></div></details>
        """
        response = _page("Achievements", body, "/achievements")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/achievements/{aid}")
    def achievement_edit(aid: str, saved: str = "", _: None = Depends(auth)):
        item = find(aid)
        image = (
            f'<img src="/achievements/{_esc(aid)}/image" width="80" height="80" '
            'style="object-fit:contain;image-rendering:pixelated" alt="Achievement artwork"/>'
            if item.get("image_file") else ""
        )
        body = f"""
        {_flash_html(saved="Saved." if saved else "")}
        <div class="card">
          <p class="mono">ID: {_esc(aid)}</p>
          <p class="muted">This ID never changes when you rename or replace the artwork.</p>
          {image}
          <form method="post" action="/achievements/{_esc(aid)}" enctype="multipart/form-data">
            {fields(item)}<button type="submit">Save</button>
            <a class="btn secondary" href="/achievements">Back</a>
          </form>
          <form method="post" action="/achievements/{_esc(aid)}/remove">
            {token_field()}
            <button class="danger" type="submit" onclick="return confirm('Remove this achievement from cards and lists? Unlock history will be kept.')">Remove achievement</button>
          </form>
        </div>
        """
        response = _page("Achievements", body, "/achievements", subtitle=_esc(item["title"]))
        response.headers["Cache-Control"] = "no-store"
        return response

    async def save(aid, title, description, rule_json, image_file, enabled, position, image):
        uploaded = None
        try:
            if image and image.filename:
                data = await image.read(MAX_IMAGE_BYTES + 1)
                if len(data) > MAX_IMAGE_BYTES:
                    raise ValueError("The image must be 4 MB or smaller.")
                with Image.open(BytesIO(data)) as source:
                    if source.format not in ("PNG", "JPEG", "WEBP"):
                        raise ValueError("Upload a PNG, JPEG, or WebP image.")
                    if source.width > 1024 or source.height > 1024:
                        raise ValueError("The image must be 1024 × 1024 pixels or smaller.")
                    source.load()
                    source = source.convert("RGBA")
                    source.thumbnail((128, 128), Image.Resampling.NEAREST)
                    ach.upload_dir().mkdir(parents=True, exist_ok=True)
                    uploaded = ach.upload_dir() / f"upload-{uuid.uuid4().hex}.png"
                    source.save(uploaded)
                    image_file = uploaded.name
            return await asyncio.to_thread(ach.save_definition, dict(
                id=aid, title=title, description=description, rule_json=rule_json,
                image_file=image_file, enabled=enabled, position=position,
            ))
        except (ValueError, UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            if uploaded:
                uploaded.unlink(missing_ok=True)
            raise HTTPException(400, str(exc)) from exc

    @app.post("/achievements")
    async def achievement_add(
        token: str = Form(""), title: str = Form(...), description: str = Form(...),
        rule_json: str = Form(...), image_file: str = Form(""), enabled: int = Form(1),
        position: int = Form(0), image: UploadFile | None = File(None),
        _: None = Depends(auth),
    ):
        check_token(token)
        aid = await save(None, title, description, rule_json, image_file, enabled, position, image)
        return RedirectResponse(f"/achievements/{aid}?saved=1", status_code=303)

    @app.post("/achievements/{aid}")
    async def achievement_save(
        aid: str, token: str = Form(""), title: str = Form(...),
        description: str = Form(...), rule_json: str = Form(...),
        image_file: str = Form(""), enabled: int = Form(1), position: int = Form(0),
        image: UploadFile | None = File(None), _: None = Depends(auth),
    ):
        check_token(token)
        find(aid)
        await save(aid, title, description, rule_json, image_file, enabled, position, image)
        return RedirectResponse(f"/achievements/{aid}?saved=1", status_code=303)

    @app.post("/achievements/{aid}/remove")
    def achievement_remove(aid: str, token: str = Form(""), _: None = Depends(auth)):
        check_token(token)
        find(aid)
        ach.remove_definition(aid)
        return RedirectResponse("/achievements?saved=1", status_code=303)

    @app.get("/achievements/{aid}/image")
    def achievement_image(aid: str, _: None = Depends(auth)):
        path = ach.image_path(find(aid).get("image_file"))
        if not path or not path.is_file():
            raise HTTPException(404, "No artwork has been uploaded.")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/achievements")
    def achievement_api(discord_id: str = "", _: None = Depends(auth)):
        items = ach.for_user(discord_id) if discord_id else ach.list_definitions(include_removed=True)
        return JSONResponse(items, headers={"Cache-Control": "no-store"})
