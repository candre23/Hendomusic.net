#!/usr/bin/env python3
"""
Hendo Music Thumbnail Tool

Purpose:
- Watches a download folder for image files.
- Lets you select an image, choose a square crop, and export it as a 200x200 JPG.
- If the source is .jpg, it overwrites the original.
- If the source is another format, it writes a .jpg version and deletes the original.
- Copies the final WordPress media URL for the selected file.

Requirements:
    pip install pillow

Optional, for instant filesystem updates:
    pip install watchdog

Run:
    python hendo_thumb_tool.py

The app defaults to your Downloads folder. You can change it inside the app.
"""

import os
import sys
import time
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from PIL import Image, ImageTk, ImageOps, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except Exception:
    HAS_WATCHDOG = False


IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".avif"
}

BASE_URL = "https://www.hendomusic.net/wp-content/media/"
OUTPUT_SIZE = 200
JPEG_QUALITY = 92


def default_downloads_folder() -> Path:
    home = Path.home()
    downloads = home / "Downloads"
    return downloads if downloads.exists() else home


def is_image_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS


def stable_file(path: Path, checks: int = 3, delay: float = 0.15) -> bool:
    """
    Helps avoid opening files that are still being written by the browser.
    """
    try:
        last_size = path.stat().st_size
        for _ in range(checks):
            time.sleep(delay)
            new_size = path.stat().st_size
            if new_size != last_size:
                last_size = new_size
            else:
                return True
    except OSError:
        return False
    return True


class FolderChangeHandler(FileSystemEventHandler):
    def __init__(self, callback):
        self.callback = callback

    def on_any_event(self, event):
        self.callback()


class ThumbnailTool(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Hendo Music Thumbnail Tool")
        self.geometry("1180x760")
        self.minsize(900, 600)

        self.folder = default_downloads_folder()
        self.current_path = None
        self.current_output_path = None

        self.original_image = None
        self.display_image = None
        self.display_photo = None

        self.img_w = 0
        self.img_h = 0
        self.canvas_img_x = 0
        self.canvas_img_y = 0
        self.display_w = 0
        self.display_h = 0
        self.display_scale = 1.0

        self.crop = None  # [x1, y1, x2, y2] in original image coordinates
        self.drag_mode = None
        self.drag_start = None
        self.crop_start = None

        self.file_paths = []
        self.refresh_pending = False
        self.observer = None
        self.poll_after_id = None

        self._build_ui()
        self._start_folder_watch()
        self.refresh_file_list()

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self):
        main = ttk.Frame(self)
        main.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(main, width=310)
        left.pack(side=tk.LEFT, fill=tk.Y)
        left.pack_propagate(False)

        folder_frame = ttk.Frame(left)
        folder_frame.pack(fill=tk.X, padx=8, pady=8)

        ttk.Label(folder_frame, text="Image folder").pack(anchor=tk.W)
        self.folder_label = ttk.Label(folder_frame, text=str(self.folder), wraplength=285)
        self.folder_label.pack(anchor=tk.W, pady=(2, 6))

        change_button = ttk.Button(folder_frame, text="Change folder", command=self.change_folder)
        change_button.pack(fill=tk.X)

        refresh_button = ttk.Button(folder_frame, text="Refresh", command=self.refresh_file_list)
        refresh_button.pack(fill=tk.X, pady=(6, 0))

        self.watch_label = ttk.Label(
            folder_frame,
            text="Real-time watching: on" if HAS_WATCHDOG else "Real-time watching: polling fallback",
        )
        self.watch_label.pack(anchor=tk.W, pady=(6, 0))

        list_frame = ttk.Frame(left)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        ttk.Label(list_frame, text="Images").pack(anchor=tk.W)

        self.file_list = tk.Listbox(list_frame, activestyle="dotbox")
        self.file_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.file_list.bind("<<ListboxSelect>>", self.on_file_select)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.file_list.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.file_list.configure(yscrollcommand=scrollbar.set)

        right = ttk.Frame(main)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        toolbar = ttk.Frame(right)
        toolbar.pack(fill=tk.X, padx=8, pady=8)

        self.apply_button = ttk.Button(toolbar, text="Apply", command=self.apply_crop, state=tk.DISABLED)
        self.apply_button.pack(side=tk.LEFT)

        self.copy_button = ttk.Button(toolbar, text="Copy URL", command=self.copy_url, state=tk.DISABLED)
        self.copy_button.pack(side=tk.LEFT, padx=(8, 0))

        self.reset_button = ttk.Button(toolbar, text="Reset crop", command=self.reset_crop, state=tk.DISABLED)
        self.reset_button.pack(side=tk.LEFT, padx=(8, 0))

        self.status = ttk.Label(toolbar, text="Select an image to begin.")
        self.status.pack(side=tk.LEFT, padx=(14, 0), fill=tk.X, expand=True)

        canvas_frame = ttk.Frame(right)
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self.canvas = tk.Canvas(canvas_frame, bg="#222222", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", self.on_canvas_resize)
        self.canvas.bind("<ButtonPress-1>", self.on_mouse_down)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_up)
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        self.canvas.bind("<Button-4>", self.on_mouse_wheel)
        self.canvas.bind("<Button-5>", self.on_mouse_wheel)

        hint = ttk.Label(
            right,
            text="Drag inside the square to move it. Drag a corner or edge to resize. Mouse wheel also resizes the crop square.",
        )
        hint.pack(fill=tk.X, padx=8, pady=(0, 8))

    def change_folder(self):
        selected = filedialog.askdirectory(initialdir=str(self.folder), title="Select image folder")
        if selected:
            self.folder = Path(selected)
            self.folder_label.configure(text=str(self.folder))
            self._start_folder_watch()
            self.refresh_file_list()

    def _start_folder_watch(self):
        if self.observer:
            try:
                self.observer.stop()
                self.observer.join(timeout=1)
            except Exception:
                pass
            self.observer = None

        if HAS_WATCHDOG:
            handler = FolderChangeHandler(self.schedule_refresh)
            self.observer = Observer()
            self.observer.schedule(handler, str(self.folder), recursive=False)
            self.observer.daemon = True
            self.observer.start()
        else:
            if self.poll_after_id:
                self.after_cancel(self.poll_after_id)
            self.poll_folder()

    def poll_folder(self):
        self.refresh_file_list(preserve_selection=True, quiet=True)
        self.poll_after_id = self.after(1500, self.poll_folder)

    def schedule_refresh(self):
        if not self.refresh_pending:
            self.refresh_pending = True
            self.after(300, self._scheduled_refresh)

    def _scheduled_refresh(self):
        self.refresh_pending = False
        self.refresh_file_list(preserve_selection=True, quiet=True)

    def refresh_file_list(self, preserve_selection=False, quiet=False):
        previous = self.current_path.name if self.current_path else None

        try:
            files = [p for p in self.folder.iterdir() if is_image_file(p)]
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception as exc:
            if not quiet:
                messagebox.showerror("Folder error", f"Could not read folder:\n{exc}")
            return

        self.file_paths = files
        self.file_list.delete(0, tk.END)

        for path in self.file_paths:
            self.file_list.insert(tk.END, path.name)

        if preserve_selection and previous:
            for i, path in enumerate(self.file_paths):
                if path.name == previous:
                    self.file_list.selection_set(i)
                    self.file_list.see(i)
                    break

        if not quiet:
            self.status.configure(text=f"Found {len(self.file_paths)} image file(s).")

    def on_file_select(self, _event=None):
        selection = self.file_list.curselection()
        if not selection:
            return

        index = selection[0]
        if index >= len(self.file_paths):
            return

        path = self.file_paths[index]
        self.load_image(path)

    def load_image(self, path: Path):
        if not path.exists():
            self.refresh_file_list()
            return

        if not stable_file(path):
            self.status.configure(text="File is still downloading. Try again in a moment.")
            return

        try:
            img = Image.open(path)
            img = ImageOps.exif_transpose(img)
            img.load()

            # Convert animated GIF/WebP to the first frame.
            if getattr(img, "is_animated", False):
                img.seek(0)
                img = img.copy()

            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")

        except Exception as exc:
            messagebox.showerror("Image error", f"Could not open image:\n{path.name}\n\n{exc}")
            return

        self.current_path = path
        self.current_output_path = self.output_path_for(path)
        self.original_image = img
        self.img_w, self.img_h = img.size

        self.reset_crop()
        self.apply_button.configure(state=tk.NORMAL)
        self.copy_button.configure(state=tk.NORMAL)
        self.reset_button.configure(state=tk.NORMAL)
        self.status.configure(text=f"Loaded {path.name}  |  {self.img_w}x{self.img_h}")
        self.render_image()

    def output_path_for(self, path: Path) -> Path:
        if path.suffix.lower() == ".jpg":
            return path
        return path.with_suffix(".jpg")

    def reset_crop(self):
        if not self.original_image:
            return

        side = min(self.img_w, self.img_h)
        x1 = (self.img_w - side) / 2
        y1 = (self.img_h - side) / 2
        self.crop = [x1, y1, x1 + side, y1 + side]
        self.render_image()

    def on_canvas_resize(self, _event=None):
        self.render_image()

    def render_image(self):
        self.canvas.delete("all")

        if not self.original_image:
            self.canvas.create_text(
                self.canvas.winfo_width() // 2,
                self.canvas.winfo_height() // 2,
                text="Select an image from the folder list",
                fill="white",
                font=("Segoe UI", 16),
            )
            return

        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())

        margin = 24
        scale = min((cw - margin * 2) / self.img_w, (ch - margin * 2) / self.img_h, 1.0)
        scale = max(scale, 0.01)

        self.display_scale = scale
        self.display_w = max(1, int(self.img_w * scale))
        self.display_h = max(1, int(self.img_h * scale))
        self.canvas_img_x = (cw - self.display_w) // 2
        self.canvas_img_y = (ch - self.display_h) // 2

        display = self.original_image.copy()
        if display.mode == "RGBA":
            bg = Image.new("RGBA", display.size, (255, 255, 255, 255))
            display = Image.alpha_composite(bg, display).convert("RGB")
        else:
            display = display.convert("RGB")

        display = display.resize((self.display_w, self.display_h), Image.Resampling.LANCZOS)
        self.display_photo = ImageTk.PhotoImage(display)
        self.canvas.create_image(self.canvas_img_x, self.canvas_img_y, anchor=tk.NW, image=self.display_photo)

        self.draw_crop_overlay()

    def image_to_canvas(self, x, y):
        return self.canvas_img_x + x * self.display_scale, self.canvas_img_y + y * self.display_scale

    def canvas_to_image(self, x, y):
        return (x - self.canvas_img_x) / self.display_scale, (y - self.canvas_img_y) / self.display_scale

    def draw_crop_overlay(self):
        if not self.crop:
            return

        x1, y1, x2, y2 = self.crop
        cx1, cy1 = self.image_to_canvas(x1, y1)
        cx2, cy2 = self.image_to_canvas(x2, y2)

        # Darken outside crop.
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()

        self.canvas.create_rectangle(0, 0, cw, cy1, fill="black", stipple="gray50", outline="")
        self.canvas.create_rectangle(0, cy2, cw, ch, fill="black", stipple="gray50", outline="")
        self.canvas.create_rectangle(0, cy1, cx1, cy2, fill="black", stipple="gray50", outline="")
        self.canvas.create_rectangle(cx2, cy1, cw, cy2, fill="black", stipple="gray50", outline="")

        self.canvas.create_rectangle(cx1, cy1, cx2, cy2, outline="white", width=2)
        self.canvas.create_rectangle(cx1, cy1, cx2, cy2, outline="black", width=1, dash=(4, 4))

        # Rule-of-thirds guides.
        third = (cx2 - cx1) / 3
        self.canvas.create_line(cx1 + third, cy1, cx1 + third, cy2, fill="white", dash=(2, 4))
        self.canvas.create_line(cx1 + third * 2, cy1, cx1 + third * 2, cy2, fill="white", dash=(2, 4))
        self.canvas.create_line(cx1, cy1 + third, cx2, cy1 + third, fill="white", dash=(2, 4))
        self.canvas.create_line(cx1, cy1 + third * 2, cx2, cy1 + third * 2, fill="white", dash=(2, 4))

        # Handles.
        handle = 8
        points = [
            (cx1, cy1), ((cx1 + cx2) / 2, cy1), (cx2, cy1),
            (cx1, (cy1 + cy2) / 2), (cx2, (cy1 + cy2) / 2),
            (cx1, cy2), ((cx1 + cx2) / 2, cy2), (cx2, cy2),
        ]
        for px, py in points:
            self.canvas.create_rectangle(
                px - handle / 2,
                py - handle / 2,
                px + handle / 2,
                py + handle / 2,
                fill="white",
                outline="black",
            )

    def hit_test_crop(self, canvas_x, canvas_y):
        if not self.crop:
            return None

        x1, y1, x2, y2 = self.crop
        cx1, cy1 = self.image_to_canvas(x1, y1)
        cx2, cy2 = self.image_to_canvas(x2, y2)

        tol = 12
        near_left = abs(canvas_x - cx1) <= tol
        near_right = abs(canvas_x - cx2) <= tol
        near_top = abs(canvas_y - cy1) <= tol
        near_bottom = abs(canvas_y - cy2) <= tol

        inside_x = cx1 <= canvas_x <= cx2
        inside_y = cy1 <= canvas_y <= cy2

        if near_left or near_right or near_top or near_bottom:
            if inside_x or inside_y:
                return "resize"

        if inside_x and inside_y:
            return "move"

        return None

    def on_mouse_down(self, event):
        if not self.crop:
            return

        self.drag_mode = self.hit_test_crop(event.x, event.y)
        self.drag_start = self.canvas_to_image(event.x, event.y)
        self.crop_start = self.crop.copy()

    def on_mouse_drag(self, event):
        if not self.drag_mode or not self.crop or not self.crop_start:
            return

        img_x, img_y = self.canvas_to_image(event.x, event.y)
        start_x, start_y = self.drag_start
        dx = img_x - start_x
        dy = img_y - start_y

        x1, y1, x2, y2 = self.crop_start
        side = x2 - x1

        if self.drag_mode == "move":
            nx1 = x1 + dx
            ny1 = y1 + dy
            nx1 = max(0, min(nx1, self.img_w - side))
            ny1 = max(0, min(ny1, self.img_h - side))
            self.crop = [nx1, ny1, nx1 + side, ny1 + side]

        elif self.drag_mode == "resize":
            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2

            # Resize based on max distance from center, keeping it square.
            half = max(abs(img_x - cx), abs(img_y - cy), 20)
            half = min(half, cx, cy, self.img_w - cx, self.img_h - cy)
            if half < 20:
                half = min(self.img_w, self.img_h) / 2

            self.crop = [cx - half, cy - half, cx + half, cy + half]

        self.render_image()

    def on_mouse_up(self, _event):
        self.drag_mode = None
        self.drag_start = None
        self.crop_start = None

    def on_mouse_wheel(self, event):
        if not self.crop:
            return

        # Windows/macOS uses delta; Linux may use Button-4/Button-5.
        if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            factor = 0.92
        else:
            factor = 1.08

        x1, y1, x2, y2 = self.crop
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2
        side = (x2 - x1) * factor

        max_side = min(cx * 2, cy * 2, (self.img_w - cx) * 2, (self.img_h - cy) * 2)
        side = max(40, min(side, max_side))

        half = side / 2
        self.crop = [cx - half, cy - half, cx + half, cy + half]
        self.render_image()

    def apply_crop(self):
        if not self.original_image or not self.current_path or not self.crop:
            return

        src = self.current_path
        dst = self.output_path_for(src)

        try:
            x1, y1, x2, y2 = self.crop
            box = (
                int(round(x1)),
                int(round(y1)),
                int(round(x2)),
                int(round(y2)),
            )

            img = self.original_image.copy()

            if img.mode == "RGBA":
                bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
                img = Image.alpha_composite(bg, img).convert("RGB")
            else:
                img = img.convert("RGB")

            cropped = img.crop(box)
            cropped = cropped.resize((OUTPUT_SIZE, OUTPUT_SIZE), Image.Resampling.LANCZOS)

            temp_dst = dst.with_name(dst.stem + ".tmp.jpg")
            cropped.save(temp_dst, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)

            if src.resolve() == dst.resolve():
                os.replace(temp_dst, dst)
            else:
                if dst.exists():
                    dst.unlink()
                temp_dst.rename(dst)

                # Delete the non-JPG original after successful conversion.
                try:
                    src.unlink()
                except OSError as exc:
                    messagebox.showwarning(
                        "Saved, but original not deleted",
                        f"Saved {dst.name}, but could not delete {src.name}:\n{exc}",
                    )

            self.current_path = dst
            self.current_output_path = dst
            self.status.configure(text=f"Saved {dst.name} as {OUTPUT_SIZE}x{OUTPUT_SIZE} JPG.")
            self.refresh_file_list(preserve_selection=True, quiet=True)

            # Reload the saved image so the pane reflects the finished thumbnail.
            self.load_image(dst)

        except Exception as exc:
            messagebox.showerror("Apply error", f"Could not apply crop:\n{exc}")

    def copy_url(self):
        if not self.current_path:
            return

        output_path = self.output_path_for(self.current_path)
        url = BASE_URL + output_path.name

        self.clipboard_clear()
        self.clipboard_append(url)
        self.update()

        self.status.configure(text=f"Copied URL: {url}")

    def on_close(self):
        if self.observer:
            try:
                self.observer.stop()
                self.observer.join(timeout=1)
            except Exception:
                pass
        if self.poll_after_id:
            try:
                self.after_cancel(self.poll_after_id)
            except Exception:
                pass
        self.destroy()


def main():
    app = ThumbnailTool()
    app.mainloop()


if __name__ == "__main__":
    main()
