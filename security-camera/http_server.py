#!/usr/bin/env python3
"""
HTTP server for serving recordings and API endpoints.
"""

import json
import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import logging

logger = logging.getLogger(__name__)

# Embedded HTML for the recordings viewer UI
INDEX_HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Security Camera Recordings</title>
    <script>window.BASE_PATH = "%%BASE_PATH%%";</script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            min-height: 100vh;
        }
        .header {
            background: #16213e;
            padding: 1rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 1rem;
            border-bottom: 1px solid #0f3460;
        }
        .header h1 {
            font-size: 1.5rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        .status {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.9rem;
        }
        .status-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #4ade80;
        }
        .status-dot.recording { background: #ef4444; animation: pulse 1s infinite; }
        .status-dot.motion { background: #f59e0b; animation: pulse 0.5s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
        .filters {
            display: flex;
            gap: 1rem;
            align-items: center;
            flex-wrap: wrap;
        }
        .filters label { font-size: 0.9rem; color: #aaa; }
        .filters input, .filters select {
            background: #0f3460;
            border: 1px solid #1a4a7a;
            color: #eee;
            padding: 0.5rem;
            border-radius: 4px;
        }
        .container { padding: 1.5rem; }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 1.5rem;
        }
        .card {
            background: #16213e;
            border-radius: 8px;
            overflow: hidden;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
            border: 1px solid #0f3460;
        }
        .card:hover {
            transform: translateY(-4px);
            box-shadow: 0 8px 25px rgba(0,0,0,0.3);
        }
        .card-thumb {
            position: relative;
            aspect-ratio: 16/9;
            background: #0a0a1a;
        }
        .card-thumb img {
            width: 100%;
            height: 100%;
            object-fit: cover;
            transition: opacity 0.15s ease;
        }
        .card-thumb .screenshot-preview {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            object-fit: cover;
            opacity: 0;
            pointer-events: none;
            z-index: 2;
        }
        .card-thumb .screenshot-preview.active {
            opacity: 1;
        }
        .card-thumb:hover .screenshot-preview.active ~ .main-thumb,
        .card-thumb:hover .main-thumb:first-child {
            opacity: 0;
        }
        .screenshot-progress {
            position: absolute;
            bottom: 0;
            left: 0;
            width: 100%;
            height: 3px;
            background: rgba(0,0,0,0.5);
            display: flex;
            gap: 2px;
            padding: 0 2px;
            opacity: 0;
            transition: opacity 0.2s;
        }
        .card-thumb:hover .screenshot-progress {
            opacity: 1;
        }
        .screenshot-progress .segment {
            flex: 1;
            height: 100%;
            background: rgba(255,255,255,0.3);
            transition: background 0.1s;
        }
        .screenshot-progress .segment.active {
            background: #4ade80;
        }
        .card-thumb .play-icon {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            width: 60px;
            height: 60px;
            background: rgba(0,0,0,0.7);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            opacity: 0;
            transition: opacity 0.2s;
            z-index: 5;
        }
        .card:hover .play-icon { opacity: 1; }
        .card-thumb.previewing .play-icon { opacity: 0 !important; }
        .card-thumb.previewing .main-thumb { opacity: 0; }
        .play-icon::after {
            content: '';
            border-style: solid;
            border-width: 12px 0 12px 20px;
            border-color: transparent transparent transparent #fff;
            margin-left: 4px;
        }
        .card-thumb .duration {
            position: absolute;
            bottom: 8px;
            right: 8px;
            background: rgba(0,0,0,0.8);
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.8rem;
        }
        .card-info {
            padding: 1rem;
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
        }
        .card-details { flex: 1; }
        .card-date { font-size: 1rem; font-weight: 500; }
        .card-time { font-size: 0.85rem; color: #aaa; margin-top: 0.25rem; }
        .card-size { font-size: 0.8rem; color: #666; margin-top: 0.5rem; }
        .btn-delete {
            background: transparent;
            border: 1px solid #dc2626;
            color: #dc2626;
            padding: 0.4rem 0.6rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.8rem;
            transition: all 0.2s;
            opacity: 0;
        }
        .card:hover .btn-delete { opacity: 1; }
        .btn-delete:hover {
            background: #dc2626;
            color: #fff;
        }
        .confirm-dialog {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.8);
            z-index: 2000;
            align-items: center;
            justify-content: center;
        }
        .confirm-dialog.active { display: flex; }
        .confirm-box {
            background: #16213e;
            padding: 2rem;
            border-radius: 8px;
            text-align: center;
            max-width: 400px;
            border: 1px solid #0f3460;
        }
        .confirm-box h3 { margin-bottom: 1rem; color: #ef4444; }
        .confirm-box p { margin-bottom: 1.5rem; color: #aaa; }
        .confirm-buttons { display: flex; gap: 1rem; justify-content: center; }
        .confirm-buttons button {
            padding: 0.6rem 1.5rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 1rem;
            border: none;
        }
        .btn-cancel { background: #374151; color: #fff; }
        .btn-cancel:hover { background: #4b5563; }
        .btn-confirm-delete { background: #dc2626; color: #fff; }
        .btn-confirm-delete:hover { background: #b91c1c; }
        .btn-favorite {
            background: transparent;
            border: none;
            font-size: 1.2rem;
            cursor: pointer;
            padding: 0.3rem;
            opacity: 0.5;
            transition: all 0.2s;
        }
        .btn-favorite:hover { opacity: 1; transform: scale(1.2); }
        .btn-favorite.active { opacity: 1; }
        .card-actions {
            display: flex;
            gap: 0.5rem;
            align-items: center;
        }
        .favorite-badge {
            position: absolute;
            top: 8px;
            left: 8px;
            font-size: 1.2rem;
            text-shadow: 0 1px 3px rgba(0,0,0,0.8);
        }
        .modal-favorite {
            background: transparent;
            border: 2px solid #f59e0b;
            color: #f59e0b;
            padding: 0.5rem 1rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.9rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        .modal-favorite:hover { background: rgba(245, 158, 11, 0.1); }
        .modal-favorite.active { background: #f59e0b; color: #000; }
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.9);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }
        .modal.active { display: flex; }
        .modal-content {
            max-width: 90vw;
            max-height: 90vh;
            position: relative;
        }
        .modal video {
            max-width: 90vw;
            max-height: 80vh;
            border-radius: 8px;
        }
        .modal-close {
            position: absolute;
            top: -40px;
            right: 0;
            background: none;
            border: none;
            color: #fff;
            font-size: 2rem;
            cursor: pointer;
            padding: 0.5rem;
        }
        .modal-info {
            color: #aaa;
            text-align: center;
            margin-top: 1rem;
        }
        .modal-actions {
            display: flex;
            justify-content: center;
            gap: 1rem;
            margin-top: 1rem;
        }
        .modal-delete {
            background: #dc2626;
            border: none;
            color: #fff;
            padding: 0.5rem 1rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.9rem;
        }
        .modal-delete:hover { background: #b91c1c; }
        .modal-nav {
            position: absolute;
            top: 50%;
            transform: translateY(-50%);
            background: rgba(255,255,255,0.1);
            border: none;
            color: #fff;
            font-size: 2rem;
            padding: 1rem;
            cursor: pointer;
            border-radius: 4px;
        }
        .modal-nav:hover { background: rgba(255,255,255,0.2); }
        .modal-nav.prev { left: -60px; }
        .modal-nav.next { right: -60px; }
        .empty {
            text-align: center;
            padding: 4rem;
            color: #666;
        }
        .empty-icon { font-size: 4rem; margin-bottom: 1rem; }
        .stats {
            display: flex;
            gap: 2rem;
            margin-bottom: 1.5rem;
            flex-wrap: wrap;
        }
        .stat {
            background: #16213e;
            padding: 1rem 1.5rem;
            border-radius: 8px;
            border: 1px solid #0f3460;
        }
        .stat-value { font-size: 1.5rem; font-weight: bold; }
        .stat-label { font-size: 0.8rem; color: #aaa; }
        /* Selection mode styles */
        .selection-bar {
            display: none;
            background: #0f3460;
            padding: 0.75rem 1.5rem;
            align-items: center;
            gap: 1rem;
            flex-wrap: wrap;
            border-bottom: 1px solid #1a4a7a;
        }
        .selection-bar.active { display: flex; }
        .selection-bar .selection-info {
            font-size: 0.9rem;
            color: #aaa;
        }
        .selection-bar .selection-count {
            color: #4ade80;
            font-weight: bold;
        }
        .selection-controls {
            display: flex;
            gap: 0.5rem;
            flex-wrap: wrap;
        }
        .btn-selection {
            background: #1a4a7a;
            border: 1px solid #2563eb;
            color: #eee;
            padding: 0.4rem 0.8rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.85rem;
            transition: all 0.2s;
        }
        .btn-selection:hover { background: #2563eb; }
        .btn-selection.danger {
            border-color: #dc2626;
            color: #ef4444;
        }
        .btn-selection.danger:hover {
            background: #dc2626;
            color: #fff;
        }
        .btn-toggle-select {
            background: transparent;
            border: 1px solid #4ade80;
            color: #4ade80;
            padding: 0.4rem 0.8rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.85rem;
            transition: all 0.2s;
        }
        .btn-toggle-select:hover { background: rgba(74, 222, 128, 0.1); }
        .btn-toggle-select.active {
            background: #4ade80;
            color: #000;
        }
        .card.selectable .card-thumb::before {
            content: '';
            position: absolute;
            top: 8px;
            right: 8px;
            width: 24px;
            height: 24px;
            border: 2px solid rgba(255,255,255,0.7);
            border-radius: 4px;
            background: rgba(0,0,0,0.5);
            z-index: 10;
            transition: all 0.2s;
        }
        .card.selectable.selected .card-thumb::before {
            background: #4ade80;
            border-color: #4ade80;
        }
        .card.selectable.selected .card-thumb::after {
            content: '✓';
            position: absolute;
            top: 8px;
            right: 8px;
            width: 24px;
            height: 24px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #000;
            font-weight: bold;
            font-size: 14px;
            z-index: 11;
        }
        .card.selectable { cursor: pointer; }
        .card.selectable .card-info { pointer-events: none; }
    </style>
</head>
<body>
    <div class="header">
        <h1>
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
            </svg>
            Security Recordings
        </h1>
        <div class="status">
            <span class="status-dot" id="statusDot"></span>
            <span id="statusText">Connecting...</span>
        </div>
        <div class="filters">
            <label>Filter:</label>
            <select id="filterFavorites">
                <option value="all">All recordings</option>
                <option value="favorites">Favorites only</option>
            </select>
            <input type="date" id="filterDate">
            <select id="filterSort">
                <option value="newest">Newest first</option>
                <option value="oldest">Oldest first</option>
            </select>
            <button class="btn-toggle-select" id="btnToggleSelect" onclick="toggleSelectionMode()">Select</button>
        </div>
    </div>
    <div class="selection-bar" id="selectionBar">
        <span class="selection-info">
            <span class="selection-count" id="selectionCount">0</span> selected
        </span>
        <div class="selection-controls">
            <button class="btn-selection" onclick="selectAll()">Select All</button>
            <button class="btn-selection" onclick="selectAllExceptFavorites()">All Except Favorites</button>
            <button class="btn-selection" onclick="deselectAll()">Deselect All</button>
            <button class="btn-selection danger" id="btnDeleteSelected" onclick="confirmDeleteSelected()" disabled>Delete Selected</button>
        </div>
    </div>
    <div class="container">
        <div class="stats" id="stats"></div>
        <div class="grid" id="grid"></div>
        <div class="empty" id="empty" style="display:none;">
            <div class="empty-icon">📹</div>
            <p>No recordings found</p>
        </div>
    </div>
    <div class="modal" id="modal">
        <div class="modal-content">
            <button class="modal-close" onclick="closeModal()">&times;</button>
            <button class="modal-nav prev" onclick="navVideo(-1)">&#8249;</button>
            <video id="player" controls></video>
            <button class="modal-nav next" onclick="navVideo(1)">&#8250;</button>
            <div class="modal-info" id="modalInfo"></div>
            <div class="modal-actions">
                <button class="modal-favorite" id="modalFavorite" onclick="toggleFavoriteCurrent()">
                    <span id="modalFavIcon">☆</span> <span id="modalFavText">Add to Favorites</span>
                </button>
                <button class="modal-delete" onclick="confirmDeleteCurrent()">Delete Recording</button>
            </div>
        </div>
    </div>
    <div class="confirm-dialog" id="confirmDialog">
        <div class="confirm-box">
            <h3>Delete Recording?</h3>
            <p id="confirmMessage">This will permanently delete the recording.</p>
            <div class="confirm-buttons">
                <button class="btn-cancel" onclick="cancelDelete()">Cancel</button>
                <button class="btn-confirm-delete" onclick="executeDelete()">Delete</button>
            </div>
        </div>
    </div>
    <script>
        let recordings = [];
        let filteredRecordings = [];
        let currentIndex = 0;
        let selectionMode = false;
        let selectedFiles = new Set();

        function formatDate(ts) {
            const d = new Date(ts * 1000);
            return d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
        }
        function formatTime(ts) {
            const d = new Date(ts * 1000);
            return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        }
        function formatDuration(secs) {
            if (!secs) return '--:--';
            const m = Math.floor(secs / 60);
            const s = Math.floor(secs % 60);
            return `${m}:${s.toString().padStart(2, '0')}`;
        }
        function formatSize(bytes) {
            if (!bytes) return '';
            const mb = bytes / 1024 / 1024;
            return `${mb.toFixed(1)} MB`;
        }
        function getDateString(ts) {
            const d = new Date(ts * 1000);
            return d.toISOString().split('T')[0];
        }

        // Get base path for ingress support
        const basePath = window.BASE_PATH || '';

        async function loadRecordings() {
            try {
                const res = await fetch(basePath + '/api/recordings');
                recordings = await res.json();
                applyFilters();
            } catch (e) {
                console.error('Failed to load recordings:', e);
            }
        }

        async function loadState() {
            try {
                const res = await fetch(basePath + '/api/state');
                const state = await res.json();
                const dot = document.getElementById('statusDot');
                const text = document.getElementById('statusText');
                dot.className = 'status-dot';
                if (state.recording?.is_recording) {
                    dot.classList.add('recording');
                    text.textContent = 'Recording...';
                } else if (state.motion?.detected) {
                    dot.classList.add('motion');
                    text.textContent = 'Motion detected';
                } else {
                    text.textContent = 'Monitoring';
                }
            } catch (e) {
                document.getElementById('statusText').textContent = 'Offline';
            }
        }

        function applyFilters() {
            const dateFilter = document.getElementById('filterDate').value;
            const sort = document.getElementById('filterSort').value;
            const favFilter = document.getElementById('filterFavorites').value;

            filteredRecordings = [...recordings];

            if (favFilter === 'favorites') {
                filteredRecordings = filteredRecordings.filter(r => r.favorite);
            }

            if (dateFilter) {
                filteredRecordings = filteredRecordings.filter(r => getDateString(r.start_time) === dateFilter);
            }

            filteredRecordings.sort((a, b) => sort === 'newest' ? b.start_time - a.start_time : a.start_time - b.start_time);

            renderGrid();
            renderStats();
        }

        function renderStats() {
            const totalSize = recordings.reduce((sum, r) => sum + (r.filesize || 0), 0);
            const totalDuration = recordings.reduce((sum, r) => sum + (r.duration || 0), 0);
            const totalFavorites = recordings.filter(r => r.favorite).length;
            document.getElementById('stats').innerHTML = `
                <div class="stat"><div class="stat-value">${recordings.length}</div><div class="stat-label">Total Recordings</div></div>
                <div class="stat"><div class="stat-value">${totalFavorites} ★</div><div class="stat-label">Favorites</div></div>
                <div class="stat"><div class="stat-value">${formatSize(totalSize)}</div><div class="stat-label">Total Size</div></div>
                <div class="stat"><div class="stat-value">${filteredRecordings.length}</div><div class="stat-label">Showing</div></div>
            `;
        }

        function renderGrid() {
            const grid = document.getElementById('grid');
            const empty = document.getElementById('empty');

            if (filteredRecordings.length === 0) {
                grid.innerHTML = '';
                empty.style.display = 'block';
                return;
            }
            empty.style.display = 'none';

            grid.innerHTML = filteredRecordings.map((r, i) => {
                const thumbName = r.thumbnail ? r.thumbnail.split('/').pop() : r.filename.replace('.mp4', '.jpg');
                const screenshots = r.screenshots || [thumbName];
                const isFav = r.favorite || false;
                const isSelected = selectedFiles.has(r.filename);

                // Build screenshot preview images
                const screenshotImgs = screenshots.map((s, idx) =>
                    `<img class="screenshot-preview" data-idx="${idx}" src="${basePath}/${s}" alt="Preview ${idx + 1}">`
                ).join('');

                // Build progress segments
                const progressSegments = screenshots.map((_, idx) =>
                    `<div class="segment" data-idx="${idx}"></div>`
                ).join('');

                // Card classes based on selection mode
                const cardClasses = ['card'];
                if (selectionMode) cardClasses.push('selectable');
                if (isSelected) cardClasses.push('selected');

                // Click handler based on selection mode
                const thumbClick = selectionMode
                    ? `onclick="toggleSelection('${r.filename}', event)"`
                    : `onclick="openModal(${i})"`;

                return `
                    <div class="${cardClasses.join(' ')}" data-filename="${r.filename}">
                        <div class="card-thumb" data-index="${i}" ${thumbClick} onmouseenter="startPreview(this)" onmouseleave="stopPreview(this)">
                            <img class="main-thumb" src="${basePath}/${thumbName}" alt="Thumbnail" onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 320 180%22><rect fill=%22%23333%22 width=%22320%22 height=%22180%22/><text x=%22160%22 y=%2290%22 fill=%22%23666%22 text-anchor=%22middle%22>No preview</text></svg>'">
                            ${screenshotImgs}
                            ${isFav ? '<div class="favorite-badge">★</div>' : ''}
                            <div class="play-icon"></div>
                            <div class="duration">${formatDuration(r.duration)}</div>
                            ${screenshots.length > 1 ? `<div class="screenshot-progress">${progressSegments}</div>` : ''}
                        </div>
                        <div class="card-info">
                            <div class="card-details" onclick="${selectionMode ? `toggleSelection('${r.filename}', event)` : `openModal(${i})`}">
                                <div class="card-date">${formatDate(r.start_time)}</div>
                                <div class="card-time">${formatTime(r.start_time)}</div>
                                <div class="card-size">${formatSize(r.filesize)}</div>
                            </div>
                            <div class="card-actions" style="${selectionMode ? 'display:none' : ''}">
                                <button class="btn-favorite ${isFav ? 'active' : ''}" onclick="toggleFavorite('${r.filename}', event)" title="${isFav ? 'Remove from favorites' : 'Add to favorites'}">${isFav ? '★' : '☆'}</button>
                                <button class="btn-delete" onclick="confirmDelete('${r.filename}', event)">Delete</button>
                            </div>
                        </div>
                    </div>
                `;
            }).join('');
        }

        // Preview hover state
        const previewIntervals = new Map();

        function startPreview(thumbEl) {
            const index = parseInt(thumbEl.dataset.index);
            const r = filteredRecordings[index];
            const screenshots = r.screenshots || [];

            if (screenshots.length <= 1) return;

            // Add previewing class to hide play icon
            thumbEl.classList.add('previewing');

            let currentIdx = 0;
            const previewImgs = thumbEl.querySelectorAll('.screenshot-preview');
            const segments = thumbEl.querySelectorAll('.segment');

            // Show first screenshot immediately
            updatePreview(previewImgs, segments, currentIdx);

            // Cycle through screenshots
            const interval = setInterval(() => {
                currentIdx = (currentIdx + 1) % screenshots.length;
                updatePreview(previewImgs, segments, currentIdx);
            }, 600); // 600ms per screenshot for smooth preview

            previewIntervals.set(thumbEl, interval);
        }

        function stopPreview(thumbEl) {
            // Remove previewing class to show play icon again
            thumbEl.classList.remove('previewing');

            const interval = previewIntervals.get(thumbEl);
            if (interval) {
                clearInterval(interval);
                previewIntervals.delete(thumbEl);
            }

            // Reset all previews
            thumbEl.querySelectorAll('.screenshot-preview').forEach(img => img.classList.remove('active'));
            thumbEl.querySelectorAll('.segment').forEach(seg => seg.classList.remove('active'));
        }

        function updatePreview(previewImgs, segments, activeIdx) {
            previewImgs.forEach((img, idx) => {
                img.classList.toggle('active', idx === activeIdx);
            });
            segments.forEach((seg, idx) => {
                seg.classList.toggle('active', idx === activeIdx);
            });
        }

        function openModal(index) {
            currentIndex = index;
            const r = filteredRecordings[index];
            const player = document.getElementById('player');
            player.src = basePath + '/' + r.filename;
            document.getElementById('modalInfo').textContent = `${formatDate(r.start_time)} at ${formatTime(r.start_time)} - ${formatDuration(r.duration)}`;
            updateModalFavorite(r.favorite);
            document.getElementById('modal').classList.add('active');
            player.play();
        }

        function updateModalFavorite(isFav) {
            const btn = document.getElementById('modalFavorite');
            const icon = document.getElementById('modalFavIcon');
            const text = document.getElementById('modalFavText');
            if (isFav) {
                btn.classList.add('active');
                icon.textContent = '★';
                text.textContent = 'Favorited';
            } else {
                btn.classList.remove('active');
                icon.textContent = '☆';
                text.textContent = 'Add to Favorites';
            }
        }

        function closeModal() {
            document.getElementById('modal').classList.remove('active');
            document.getElementById('player').pause();
        }

        function navVideo(dir) {
            currentIndex = (currentIndex + dir + filteredRecordings.length) % filteredRecordings.length;
            openModal(currentIndex);
        }

        document.addEventListener('keydown', (e) => {
            if (!document.getElementById('modal').classList.contains('active')) return;
            if (e.key === 'Escape') closeModal();
            if (e.key === 'ArrowLeft') navVideo(-1);
            if (e.key === 'ArrowRight') navVideo(1);
        });

        document.getElementById('modal').addEventListener('click', (e) => {
            if (e.target.id === 'modal') closeModal();
        });

        document.getElementById('filterDate').addEventListener('change', applyFilters);
        document.getElementById('filterSort').addEventListener('change', applyFilters);
        document.getElementById('filterFavorites').addEventListener('change', applyFilters);

        async function toggleFavorite(filename, event) {
            if (event) event.stopPropagation();
            try {
                const res = await fetch(basePath + `/api/recordings/${filename}/favorite`, { method: 'POST' });
                if (res.ok) {
                    const data = await res.json();
                    // Update local data
                    const rec = recordings.find(r => r.filename === filename);
                    if (rec) rec.favorite = data.favorite;
                    applyFilters();
                }
            } catch (e) {
                console.error('Failed to toggle favorite:', e);
            }
        }

        async function toggleFavoriteCurrent() {
            const r = filteredRecordings[currentIndex];
            if (!r) return;
            await toggleFavorite(r.filename);
            // Update modal UI
            const rec = recordings.find(x => x.filename === r.filename);
            if (rec) updateModalFavorite(rec.favorite);
        }

        let deleteFilename = null;

        function confirmDelete(filename, event) {
            if (event) event.stopPropagation();
            deleteFilename = filename;
            document.getElementById('confirmMessage').textContent = `This will permanently delete "${filename}"`;
            document.getElementById('confirmDialog').classList.add('active');
        }

        function confirmDeleteCurrent() {
            const r = filteredRecordings[currentIndex];
            if (r) confirmDelete(r.filename);
        }

        function cancelDelete() {
            deleteFilename = null;
            document.getElementById('confirmDialog').classList.remove('active');
        }

        async function executeDelete() {
            // Check if this is a bulk delete operation
            if (bulkDeleteFiles.length > 0) {
                await executeBulkDelete();
                return;
            }

            if (!deleteFilename) return;
            try {
                // Use POST /api/recordings/{filename}/delete for ingress compatibility
                // (Home Assistant ingress converts DELETE to POST)
                const res = await fetch(basePath + `/api/recordings/${deleteFilename}/delete`, { method: 'POST' });
                if (res.ok) {
                    cancelDelete();
                    closeModal();
                    await loadRecordings();
                } else {
                    const err = await res.text();
                    alert(`Failed to delete: ${err}`);
                }
            } catch (e) {
                alert(`Error: ${e.message}`);
            }
        }

        // Selection mode functions
        function toggleSelectionMode() {
            selectionMode = !selectionMode;
            const btn = document.getElementById('btnToggleSelect');
            const bar = document.getElementById('selectionBar');

            if (selectionMode) {
                btn.classList.add('active');
                btn.textContent = 'Cancel';
                bar.classList.add('active');
            } else {
                btn.classList.remove('active');
                btn.textContent = 'Select';
                bar.classList.remove('active');
                selectedFiles.clear();
            }
            renderGrid();
            updateSelectionCount();
        }

        function toggleSelection(filename, event) {
            if (event) event.stopPropagation();
            if (!selectionMode) return;

            if (selectedFiles.has(filename)) {
                selectedFiles.delete(filename);
            } else {
                selectedFiles.add(filename);
            }

            // Update card UI
            const cards = document.querySelectorAll('.card.selectable');
            cards.forEach(card => {
                const cardFilename = card.dataset.filename;
                if (cardFilename === filename) {
                    card.classList.toggle('selected', selectedFiles.has(filename));
                }
            });

            updateSelectionCount();
        }

        function selectAll() {
            filteredRecordings.forEach(r => selectedFiles.add(r.filename));
            renderGrid();
            updateSelectionCount();
        }

        function selectAllExceptFavorites() {
            filteredRecordings.forEach(r => {
                if (!r.favorite) {
                    selectedFiles.add(r.filename);
                } else {
                    selectedFiles.delete(r.filename);
                }
            });
            renderGrid();
            updateSelectionCount();
        }

        function deselectAll() {
            selectedFiles.clear();
            renderGrid();
            updateSelectionCount();
        }

        function updateSelectionCount() {
            const count = selectedFiles.size;
            document.getElementById('selectionCount').textContent = count;
            const deleteBtn = document.getElementById('btnDeleteSelected');
            deleteBtn.disabled = count === 0;
            if (count > 0) {
                deleteBtn.textContent = `Delete Selected (${count})`;
            } else {
                deleteBtn.textContent = 'Delete Selected';
            }
        }

        let bulkDeleteFiles = [];

        function confirmDeleteSelected() {
            if (selectedFiles.size === 0) return;
            bulkDeleteFiles = Array.from(selectedFiles);
            const favCount = bulkDeleteFiles.filter(f => {
                const rec = recordings.find(r => r.filename === f);
                return rec && rec.favorite;
            }).length;

            let message = `This will permanently delete ${bulkDeleteFiles.length} recording(s).`;
            if (favCount > 0) {
                message += ` Including ${favCount} favorite(s).`;
            }
            document.getElementById('confirmMessage').textContent = message;
            document.getElementById('confirmDialog').classList.add('active');
        }

        async function executeBulkDelete() {
            if (bulkDeleteFiles.length === 0) return;

            const payload = { filenames: bulkDeleteFiles };
            console.log('Bulk delete payload:', payload);
            console.log('Bulk delete URL:', basePath + '/api/recordings/bulk-delete');

            try {
                const res = await fetch(basePath + '/api/recordings/bulk-delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                console.log('Bulk delete response status:', res.status);

                if (res.ok) {
                    const result = await res.json();
                    selectedFiles.clear();
                    bulkDeleteFiles = [];
                    cancelDelete();
                    await loadRecordings();
                    updateSelectionCount();
                } else {
                    const err = await res.text();
                    alert(`Failed to delete: ${err}`);
                }
            } catch (e) {
                alert(`Error: ${e.message}`);
            }
        }

        loadRecordings();
        loadState();
        setInterval(loadState, 5000);
        setInterval(loadRecordings, 30000);
    </script>
</body>
</html>
'''


class SecurityHTTPHandler(SimpleHTTPRequestHandler):
    """HTTP handler with CORS and API endpoints."""

    recordings_path = "/share/security_recordings"
    state_file = "/share/security_state.json"
    settings_file = "/share/security_settings.json"

    def __init__(self, *args, **kwargs):
        # Set directory to recordings path
        super().__init__(*args, directory=self.recordings_path, **kwargs)

    def handle(self):
        """Handle request with graceful connection error handling."""
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError) as e:
            # Client disconnected mid-transfer - expected behavior, don't spam logs
            logger.debug(f"Client disconnected: {e}")

    def log_message(self, format, *args):
        """Log to stderr for Home Assistant."""
        logger.info("%s - %s", self.address_string(), format % args)

    def send_cors_headers(self):
        """Add CORS headers for cross-origin requests."""
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')

    def do_OPTIONS(self):
        """Handle preflight CORS requests."""
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        """Handle GET requests."""
        # Serve index page at root
        if self.path == '/' or self.path == '/index.html':
            self.handle_index()
        # API endpoints
        elif self.path == '/api/state':
            self.handle_api_state()
        elif self.path == '/api/recordings':
            self.handle_api_recordings()
        elif self.path == '/api/health':
            self.handle_api_health()
        elif self.path == '/api/settings':
            self.handle_api_get_settings()
        elif self.path.startswith('/api/'):
            self.send_error(404, "API endpoint not found")
        else:
            # Serve static files (recordings, thumbnails)
            super().do_GET()

    def handle_index(self):
        """Serve the recordings viewer UI."""
        # Get ingress path from Home Assistant header (empty string if not using ingress)
        ingress_path = self.headers.get('X-Ingress-Path', '')

        # Inject the base path into the HTML template
        html = INDEX_HTML_TEMPLATE.replace('%%BASE_PATH%%', ingress_path)

        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def do_POST(self):
        """Handle POST requests."""
        # Parse path to handle query strings properly
        parsed_path = urlparse(self.path).path
        logger.debug(f"POST request path: {self.path} -> parsed: {parsed_path}")

        if parsed_path == '/api/settings':
            self.handle_api_set_settings()
        elif parsed_path.startswith('/api/settings/'):
            self.handle_api_quick_settings()
        elif parsed_path == '/api/recordings/bulk-delete':
            self.handle_api_bulk_delete()
        elif parsed_path.endswith('/delete'):
            # POST-based single file delete for ingress compatibility (HA ingress converts DELETE to POST)
            self.handle_api_delete_recording_post()
        elif parsed_path.endswith('/favorite'):
            self.handle_api_toggle_favorite()
        else:
            self.send_error(404, "API endpoint not found")

    def do_DELETE(self):
        """Handle DELETE requests."""
        if self.path.startswith('/api/recordings/'):
            self.handle_api_delete_recording()
        else:
            self.send_error(404, "API endpoint not found")

    def end_headers(self):
        """Add CORS headers to all responses."""
        self.send_cors_headers()
        super().end_headers()

    def handle_api_state(self):
        """Return current sensor state."""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
            else:
                state = {"error": "State file not found"}

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(state, indent=2).encode())

        except Exception as e:
            self.send_error(500, str(e))

    def handle_api_recordings(self):
        """Return list of recordings."""
        try:
            metadata_file = Path(self.recordings_path) / "recordings.json"
            if metadata_file.exists():
                with open(metadata_file, 'r') as f:
                    recordings = json.load(f)
            else:
                recordings = []

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(recordings, indent=2).encode())

        except Exception as e:
            self.send_error(500, str(e))

    def handle_api_delete_recording_post(self):
        """Delete a recording by filename via POST (for ingress compatibility): POST /api/recordings/{filename}/delete"""
        try:
            # Extract filename from path: /api/recordings/{filename}/delete
            parsed_path = urlparse(self.path).path
            parts = parsed_path.split('/')
            if len(parts) < 5:
                self.send_error(400, "Filename required")
                return

            filename = parts[3]
            self._delete_recording_by_filename(filename)
        except Exception as e:
            logger.error(f"Error deleting recording (POST): {e}")
            self.send_error(500, str(e))

    def handle_api_delete_recording(self):
        """Delete a recording by filename: DELETE /api/recordings/{filename}"""
        try:
            # Extract filename from path
            parts = self.path.split('/')
            if len(parts) < 4:
                self.send_error(400, "Filename required")
                return

            filename = parts[3]
            self._delete_recording_by_filename(filename)
        except Exception as e:
            logger.error(f"Error deleting recording: {e}")
            self.send_error(500, str(e))

    def _delete_recording_by_filename(self, filename: str):
        """Shared logic to delete a recording by filename."""
        if not filename.endswith('.mp4'):
            self.send_error(400, "Invalid filename")
            return

        # Load metadata
        metadata_file = Path(self.recordings_path) / "recordings.json"
        if not metadata_file.exists():
            self.send_error(404, "No recordings found")
            return

        with open(metadata_file, 'r') as f:
            recordings = json.load(f)

        # Find and remove the recording
        recording_to_delete = None
        for r in recordings:
            if r.get('filename') == filename:
                recording_to_delete = r
                break

        if not recording_to_delete:
            self.send_error(404, f"Recording not found: {filename}")
            return

        deleted_files = []
        recordings_dir = Path(self.recordings_path)

        # Delete video file
        video_path = recordings_dir / filename
        if video_path.exists():
            video_path.unlink()
            deleted_files.append(filename)
            logger.info(f"Deleted video: {video_path}")
        else:
            logger.warning(f"Video file not found: {video_path}")

        # Delete all screenshots from the array
        if recording_to_delete.get('screenshots'):
            for screenshot in recording_to_delete['screenshots']:
                # Handle both full path and just filename
                screenshot_name = Path(screenshot).name
                screenshot_path = recordings_dir / screenshot_name
                if screenshot_path.exists():
                    screenshot_path.unlink()
                    deleted_files.append(screenshot_name)
                    logger.info(f"Deleted screenshot: {screenshot_path}")

        # Also delete thumbnail if it's different from screenshots
        if recording_to_delete.get('thumbnail'):
            thumb_name = Path(recording_to_delete['thumbnail']).name
            thumb_path = recordings_dir / thumb_name
            if thumb_path.exists():
                thumb_path.unlink()
                if thumb_name not in deleted_files:
                    deleted_files.append(thumb_name)
                logger.info(f"Deleted thumbnail: {thumb_path}")

        # Clean up any orphan files with the same base name (e.g., motion_20241127_143022_*.jpg)
        base_name = video_path.stem  # e.g., "motion_20241127_143022"
        for orphan_file in recordings_dir.glob(f"{base_name}*.jpg"):
            if orphan_file.name not in deleted_files:
                orphan_file.unlink()
                deleted_files.append(orphan_file.name)
                logger.info(f"Deleted orphan file: {orphan_file}")

        # Update metadata - remove from list
        recordings.remove(recording_to_delete)
        with open(metadata_file, 'w') as f:
            json.dump(recordings, f, indent=2)

        logger.info(f"Recording deleted: {filename}, total files removed: {len(deleted_files)}")

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({
            "status": "ok",
            "deleted": filename,
            "files_removed": len(deleted_files)
        }).encode())

    def handle_api_bulk_delete(self):
        """Bulk delete recordings: POST /api/recordings/bulk-delete with JSON body {"filenames": [...]}"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            logger.debug(f"Bulk delete - Content-Length: {content_length}")

            if content_length == 0:
                self.send_error(400, "Request body is empty")
                return

            body = self.rfile.read(content_length).decode('utf-8')
            logger.debug(f"Bulk delete - Body: {body[:200] if body else 'EMPTY'}")

            if not body.strip():
                self.send_error(400, "Request body is empty")
                return

            try:
                data = json.loads(body)
            except json.JSONDecodeError as e:
                logger.error(f"JSON parse error: {e}, body: {body[:100]}")
                self.send_error(400, f"Invalid JSON: {e}")
                return

            filenames = data.get('filenames', [])
            if not filenames:
                self.send_error(400, "No filenames provided")
                return

            # Load metadata
            metadata_file = Path(self.recordings_path) / "recordings.json"
            if not metadata_file.exists():
                self.send_error(404, "No recordings found")
                return

            with open(metadata_file, 'r') as f:
                recordings = json.load(f)

            recordings_dir = Path(self.recordings_path)
            total_deleted = 0
            total_files_removed = 0
            errors = []

            for filename in filenames:
                if not filename.endswith('.mp4'):
                    errors.append(f"Invalid filename: {filename}")
                    continue

                # Find the recording
                recording_to_delete = None
                for r in recordings:
                    if r.get('filename') == filename:
                        recording_to_delete = r
                        break

                if not recording_to_delete:
                    errors.append(f"Recording not found: {filename}")
                    continue

                deleted_files = []

                # Delete video file
                video_path = recordings_dir / filename
                if video_path.exists():
                    video_path.unlink()
                    deleted_files.append(filename)
                    logger.info(f"Deleted video: {video_path}")

                # Delete all screenshots
                if recording_to_delete.get('screenshots'):
                    for screenshot in recording_to_delete['screenshots']:
                        screenshot_name = Path(screenshot).name
                        screenshot_path = recordings_dir / screenshot_name
                        if screenshot_path.exists():
                            screenshot_path.unlink()
                            deleted_files.append(screenshot_name)
                            logger.info(f"Deleted screenshot: {screenshot_path}")

                # Delete thumbnail if different from screenshots
                if recording_to_delete.get('thumbnail'):
                    thumb_name = Path(recording_to_delete['thumbnail']).name
                    thumb_path = recordings_dir / thumb_name
                    if thumb_path.exists():
                        thumb_path.unlink()
                        if thumb_name not in deleted_files:
                            deleted_files.append(thumb_name)
                        logger.info(f"Deleted thumbnail: {thumb_path}")

                # Clean up orphan files
                base_name = video_path.stem
                for orphan_file in recordings_dir.glob(f"{base_name}*.jpg"):
                    if orphan_file.name not in deleted_files:
                        orphan_file.unlink()
                        deleted_files.append(orphan_file.name)
                        logger.info(f"Deleted orphan file: {orphan_file}")

                # Remove from recordings list
                recordings.remove(recording_to_delete)
                total_deleted += 1
                total_files_removed += len(deleted_files)

            # Save updated metadata
            with open(metadata_file, 'w') as f:
                json.dump(recordings, f, indent=2)

            logger.info(f"Bulk delete completed: {total_deleted} recordings, {total_files_removed} files removed")

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "deleted_count": total_deleted,
                "files_removed": total_files_removed,
                "errors": errors if errors else None
            }).encode())

        except Exception as e:
            logger.error(f"Error in bulk delete: {e}")
            self.send_error(500, str(e))

    def handle_api_toggle_favorite(self):
        """Toggle favorite status: POST /api/recordings/{filename}/favorite"""
        try:
            # Extract filename from path: /api/recordings/{filename}/favorite
            parts = self.path.split('/')
            if len(parts) < 5:
                self.send_error(400, "Filename required")
                return

            filename = parts[3]
            if not filename.endswith('.mp4'):
                self.send_error(400, "Invalid filename")
                return

            # Load metadata
            metadata_file = Path(self.recordings_path) / "recordings.json"
            if not metadata_file.exists():
                self.send_error(404, "No recordings found")
                return

            with open(metadata_file, 'r') as f:
                recordings = json.load(f)

            # Find and toggle favorite
            recording = None
            for r in recordings:
                if r.get('filename') == filename:
                    recording = r
                    break

            if not recording:
                self.send_error(404, f"Recording not found: {filename}")
                return

            # Toggle favorite status
            recording['favorite'] = not recording.get('favorite', False)

            # Save metadata
            with open(metadata_file, 'w') as f:
                json.dump(recordings, f, indent=2)

            logger.info(f"Recording {filename} favorite: {recording['favorite']}")

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "filename": filename,
                "favorite": recording['favorite']
            }).encode())

        except Exception as e:
            logger.error(f"Error toggling favorite: {e}")
            self.send_error(500, str(e))

    def handle_api_health(self):
        """Health check endpoint."""
        health = {
            "status": "ok",
            "service": "security-camera-motion",
            "recordings_path": self.recordings_path,
            "state_file": self.state_file
        }

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(health, indent=2).encode())

    def _get_settings(self) -> dict:
        """Read current settings from file."""
        defaults = {
            "roi_x_start": 33,
            "roi_x_end": 66,
            "roi_y_start": 5,
            "roi_y_end": 95,
            "motion_threshold": 5000
        }
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r') as f:
                    saved = json.load(f)
                    defaults.update(saved)
        except Exception as e:
            logger.warning(f"Error reading settings: {e}")
        return defaults

    def _save_settings(self, settings: dict):
        """Save settings to file."""
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(settings, f, indent=2)
            logger.info(f"Settings saved: {settings}")
        except Exception as e:
            logger.error(f"Error saving settings: {e}")
            raise

    def handle_api_get_settings(self):
        """Return current motion detection settings."""
        try:
            settings = self._get_settings()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(settings, indent=2).encode())
        except Exception as e:
            self.send_error(500, str(e))

    def handle_api_set_settings(self):
        """Update motion detection settings via JSON body."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            new_settings = json.loads(body)

            # Validate and merge settings
            current = self._get_settings()
            if 'roi_x_start' in new_settings:
                current['roi_x_start'] = max(0, min(100, int(new_settings['roi_x_start'])))
            if 'roi_x_end' in new_settings:
                current['roi_x_end'] = max(0, min(100, int(new_settings['roi_x_end'])))
            if 'roi_y_start' in new_settings:
                current['roi_y_start'] = max(0, min(100, int(new_settings['roi_y_start'])))
            if 'roi_y_end' in new_settings:
                current['roi_y_end'] = max(0, min(100, int(new_settings['roi_y_end'])))
            if 'motion_threshold' in new_settings:
                current['motion_threshold'] = max(0, int(new_settings['motion_threshold']))

            self._save_settings(current)

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "settings": current}).encode())
        except Exception as e:
            self.send_error(400, str(e))

    def handle_api_quick_settings(self):
        """Quick settings via URL:
        /api/settings/roi/33/66 - set X axis ROI
        /api/settings/roi_y/5/95 - set Y axis ROI (crop timestamp)
        /api/settings/threshold/5000 - set threshold
        """
        try:
            parts = self.path.split('/')
            current = self._get_settings()

            if len(parts) >= 5 and parts[3] == 'roi':
                current['roi_x_start'] = max(0, min(100, int(parts[4])))
                if len(parts) >= 6:
                    current['roi_x_end'] = max(0, min(100, int(parts[5])))
            elif len(parts) >= 5 and parts[3] == 'roi_y':
                current['roi_y_start'] = max(0, min(100, int(parts[4])))
                if len(parts) >= 6:
                    current['roi_y_end'] = max(0, min(100, int(parts[5])))
            elif len(parts) >= 5 and parts[3] == 'threshold':
                current['motion_threshold'] = max(0, int(parts[4]))
            else:
                self.send_error(400, "Use /api/settings/roi/{x1}/{x2}, /api/settings/roi_y/{y1}/{y2}, or /api/settings/threshold/{value}")
                return

            self._save_settings(current)

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "settings": current}).encode())
        except Exception as e:
            self.send_error(400, str(e))


def run_server(port: int = 8081, recordings_path: str = "/share/security_recordings", state_file: str = "/share/security_state.json", settings_file: str = "/share/security_settings.json"):
    """Run the HTTP server."""
    SecurityHTTPHandler.recordings_path = recordings_path
    SecurityHTTPHandler.state_file = state_file
    SecurityHTTPHandler.settings_file = settings_file

    # Ensure directory exists
    Path(recordings_path).mkdir(parents=True, exist_ok=True)

    server = HTTPServer(('0.0.0.0', port), SecurityHTTPHandler)
    logger.info(f"HTTP server starting on port {port}")
    logger.info(f"Serving recordings from: {recordings_path}")
    logger.info(f"Settings file: {settings_file}")
    logger.info(f"API endpoints: /api/state, /api/recordings, /api/health, /api/settings")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server shutting down")
        server.shutdown()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        stream=sys.stderr
    )

    port = int(os.environ.get('HTTP_PORT', 8081))
    recordings = os.environ.get('RECORDINGS_PATH', '/share/security_recordings')
    state = os.environ.get('STATE_FILE', '/share/security_state.json')

    run_server(port, recordings, state)
