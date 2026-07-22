"use strict";

export const GIS_SCRIPT_URL = "https://accounts.google.com/gsi/client";
export const AUTH_DOM_IDS = Object.freeze([
    "header-channel",
    "auth-root",
    "google-signin-button",
    "auth-user-button",
    "auth-avatar",
    "auth-user-name",
    "auth-menu",
    "auth-history-button",
    "auth-delete-all-button",
    "auth-signout-button",
    "auth-status-wrap",
    "auth-status",
    "auth-retry-button",
    "transcription-panel",
    "transcription-eyebrow",
    "transcription-heading",
    "transcription-live",
    "past-conversations",
    "history-heading",
    "history-refresh-button",
    "history-list-view",
    "history-status",
    "history-list",
    "history-more-button",
    "history-detail-view",
    "history-back-button",
    "history-detail-title",
    "history-detail-meta",
    "history-transcript",
    "history-detail-status",
    "history-turns-more-button",
]);

const DISPLAY_CACHE_KEY = "nanoclaw.authDisplay.v1";
const DEFAULT_GIS_TIMEOUT_MS = 8000;
const DEFAULT_PAGE_SIZE = 20;
const DEFAULT_TURN_PAGE_SIZE = 50;

function stringValue(value) {
    return typeof value === "string" ? value : "";
}

function normalizedUser(value) {
    if (!value || typeof value !== "object" || typeof value.sub !== "string" || !value.sub) {
        return null;
    }
    return {
        sub: value.sub,
        tenant: stringValue(value.tenant),
        email: stringValue(value.email),
        name: stringValue(value.name),
    };
}

function displayName(user) {
    const name = stringValue(user && user.name).trim();
    if (name) return name;
    const email = stringValue(user && user.email).trim();
    if (email) return email;
    return "Signed in";
}

export function initialsForUser(user) {
    const name = stringValue(user && user.name).trim();
    const email = stringValue(user && user.email).trim();
    const source = name || email.split("@")[0] || "NC";
    const words = source.split(/\s+/u).filter(Boolean);
    const letters = words.length > 1
        ? [words[0], words[words.length - 1]].map(function (word) {
            return Array.from(word)[0] || "";
        }).join("")
        : Array.from(words[0] || "NC").slice(0, 2).join("");
    return letters.toLocaleUpperCase().slice(0, 2) || "NC";
}

export function renderIdentity(nameElement, avatarElement, user) {
    nameElement.textContent = displayName(user);
    avatarElement.textContent = initialsForUser(user);
}

export function formatRelativeTime(timestamp, nowValue, locale) {
    const parsed = new Date(timestamp);
    if (Number.isNaN(parsed.getTime())) return "Unknown time";
    const now = typeof nowValue === "number" ? nowValue : Date.now();
    const difference = parsed.getTime() - now;
    const absolute = Math.abs(difference);
    const units = [
        ["year", 365 * 24 * 60 * 60 * 1000],
        ["month", 30 * 24 * 60 * 60 * 1000],
        ["day", 24 * 60 * 60 * 1000],
        ["hour", 60 * 60 * 1000],
        ["minute", 60 * 1000],
        ["second", 1000],
    ];
    let selected = units[units.length - 1];
    for (const candidate of units) {
        if (absolute >= candidate[1] || candidate[0] === "second") {
            selected = candidate;
            break;
        }
    }
    const amount = Math.round(difference / selected[1]);
    try {
        return new Intl.RelativeTimeFormat(locale, { numeric: "auto" })
            .format(amount, selected[0]);
    } catch (_error) {
        if (amount === 0) return "just now";
        return Math.abs(amount) + " " + selected[0] + (Math.abs(amount) === 1 ? "" : "s")
            + (amount < 0 ? " ago" : " from now");
    }
}

function exactTime(timestamp, locale) {
    const parsed = new Date(timestamp);
    if (Number.isNaN(parsed.getTime())) return "";
    try {
        return parsed.toLocaleString(locale, {
            dateStyle: "medium",
            timeStyle: "short",
        });
    } catch (_error) {
        return parsed.toISOString();
    }
}

function appendTextElement(documentRef, parent, tagName, className, text) {
    const element = documentRef.createElement(tagName);
    element.className = className;
    element.textContent = text;
    parent.appendChild(element);
    return element;
}

export function createConversationListItem(documentRef, conversation, actions, options) {
    const config = options || {};
    const title = stringValue(conversation && conversation.title).trim()
        || "Untitled conversation";
    const item = documentRef.createElement("article");
    item.className = "history-item";

    const openButton = documentRef.createElement("button");
    openButton.type = "button";
    openButton.className = "history-item-open";
    openButton.addEventListener("click", function () {
        actions.onOpen(conversation);
    });
    appendTextElement(documentRef, openButton, "span", "history-item-title", title);

    const meta = documentRef.createElement("span");
    meta.className = "history-item-meta";
    const time = documentRef.createElement("time");
    time.textContent = formatRelativeTime(
        conversation && conversation.startedAt,
        config.now,
        config.locale,
    );
    time.dateTime = stringValue(conversation && conversation.startedAt);
    time.title = exactTime(conversation && conversation.startedAt, config.locale);
    meta.appendChild(time);
    if (conversation && conversation.incomplete === true) {
        appendTextElement(documentRef, meta, "span", "history-incomplete", "INCOMPLETE");
    }
    openButton.appendChild(meta);

    const deleteButton = documentRef.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "history-item-delete";
    deleteButton.textContent = "Delete";
    deleteButton.setAttribute("aria-label", "Delete conversation");
    deleteButton.addEventListener("click", function () {
        actions.onDelete(conversation, deleteButton);
    });

    item.appendChild(openButton);
    item.appendChild(deleteButton);
    return item;
}

export function createHistoryTurn(documentRef, turn, options) {
    const role = turn && turn.role === "agent" ? "agent" : "user";
    const item = documentRef.createElement("article");
    item.className = "history-turn history-turn-" + role;
    appendTextElement(
        documentRef,
        item,
        "span",
        "history-turn-speaker",
        role === "agent" ? "AGENT:" : "CALLER:",
    );
    appendTextElement(
        documentRef,
        item,
        "p",
        "history-turn-text",
        stringValue(turn && turn.text),
    );
    const timestamp = appendTextElement(
        documentRef,
        item,
        "time",
        "history-turn-time",
        exactTime(turn && turn.ts, options && options.locale),
    );
    timestamp.dateTime = stringValue(turn && turn.ts);
    return item;
}

function hasGoogleIdentity(windowRef) {
    return Boolean(
        windowRef
        && windowRef.google
        && windowRef.google.accounts
        && windowRef.google.accounts.id
        && typeof windowRef.google.accounts.id.initialize === "function"
        && typeof windowRef.google.accounts.id.renderButton === "function"
    );
}

export function loadGoogleIdentityScript(options) {
    const config = options || {};
    const documentRef = config.document;
    const windowRef = config.window;
    const timeoutMs = Number.isFinite(config.timeoutMs)
        ? config.timeoutMs
        : DEFAULT_GIS_TIMEOUT_MS;
    const setTimeoutRef = config.setTimeout || setTimeout;
    const clearTimeoutRef = config.clearTimeout || clearTimeout;

    if (hasGoogleIdentity(windowRef)) return Promise.resolve(windowRef.google.accounts.id);
    if (!documentRef || !documentRef.head) {
        return Promise.reject(new Error("Google sign-in cannot load"));
    }

    let script = documentRef.getElementById("google-identity-services");
    if (!script) {
        script = documentRef.createElement("script");
        script.id = "google-identity-services";
        script.src = GIS_SCRIPT_URL;
        script.async = true;
        script.referrerPolicy = "strict-origin-when-cross-origin";
        documentRef.head.appendChild(script);
    }

    return new Promise(function (resolve, reject) {
        let settled = false;
        let timer = null;
        const cleanup = function () {
            script.removeEventListener("load", onLoad);
            script.removeEventListener("error", onError);
            if (timer !== null) clearTimeoutRef(timer);
        };
        const finish = function (callback) {
            if (settled) return;
            settled = true;
            cleanup();
            callback();
        };
        const onLoad = function () {
            finish(function () {
                if (hasGoogleIdentity(windowRef)) resolve(windowRef.google.accounts.id);
                else reject(new Error("Google sign-in did not initialize"));
            });
        };
        const onError = function () {
            finish(function () { reject(new Error("Google sign-in was blocked")); });
        };
        script.addEventListener("load", onLoad);
        script.addEventListener("error", onError);
        timer = setTimeoutRef(function () {
            finish(function () { reject(new Error("Google sign-in timed out")); });
        }, Math.max(1, timeoutMs));
    });
}

export class AuthHistoryUI {
    constructor(options) {
        const config = options || {};
        this.document = config.document || document;
        this.window = config.window || window;
        this.fetch = config.fetch || fetch.bind(globalThis);
        this.onIdentityChange = typeof config.onIdentityChange === "function"
            ? config.onIdentityChange
            : function () {};
        this.now = typeof config.now === "function" ? config.now : Date.now;
        this.setTimeout = config.setTimeout || setTimeout;
        this.clearTimeout = config.clearTimeout || clearTimeout;
        this.gisTimeoutMs = Number.isFinite(config.gisTimeoutMs)
            ? config.gisTimeoutMs
            : DEFAULT_GIS_TIMEOUT_MS;
        this.locale = config.locale;

        this.elements = {};
        for (const id of AUTH_DOM_IDS) {
            const element = this.document.getElementById(id);
            if (!element) throw new Error("Missing auth UI element: " + id);
            this.elements[id] = element;
        }

        this.config = null;
        this.user = null;
        this.destroyed = false;
        this.authGeneration = 0;
        this.gisGeneration = 0;
        this.historyGeneration = 0;
        this.detailGeneration = 0;
        this.historyCursor = null;
        this.turnCursor = null;
        this.detailConversationId = null;
        this.historyCount = 0;
        this.historyRefreshTimer = null;
        this.gisRenderTimer = null;
        this.signInBusy = false;

        this._boundUserClick = this._toggleMenu.bind(this);
        this._boundHistoryClick = this._openHistoryFromMenu.bind(this);
        this._boundDeleteAll = this.deleteAllHistory.bind(this);
        this._boundSignOut = this.signOut.bind(this);
        this._boundRetry = this.retry.bind(this);
        this._boundRefresh = function () { this.loadConversations(false); }.bind(this);
        this._boundMore = function () { this.loadConversations(true); }.bind(this);
        this._boundBack = this.closeConversation.bind(this);
        this._boundTurnsMore = function () { this.loadConversationTurns(true); }.bind(this);
        this._boundDocumentClick = this._handleDocumentClick.bind(this);
        this._boundDocumentKeydown = this._handleDocumentKeydown.bind(this);

        this._listen("auth-user-button", "click", this._boundUserClick);
        this._listen("auth-history-button", "click", this._boundHistoryClick);
        this._listen("auth-delete-all-button", "click", this._boundDeleteAll);
        this._listen("auth-signout-button", "click", this._boundSignOut);
        this._listen("auth-retry-button", "click", this._boundRetry);
        this._listen("history-refresh-button", "click", this._boundRefresh);
        this._listen("history-more-button", "click", this._boundMore);
        this._listen("history-back-button", "click", this._boundBack);
        this._listen("history-turns-more-button", "click", this._boundTurnsMore);
        this.document.addEventListener("click", this._boundDocumentClick);
        this.document.addEventListener("keydown", this._boundDocumentKeydown);
    }

    _element(id) {
        return this.elements[id];
    }

    _listen(id, eventName, handler) {
        this._element(id).addEventListener(eventName, handler);
    }

    async _fetchJson(path, options) {
        const requestOptions = Object.assign({
            credentials: "same-origin",
            headers: { "Accept": "application/json" },
        }, options || {});
        const response = await this.fetch(path, requestOptions);
        let data = null;
        try {
            data = await response.json();
        } catch (_error) {
            data = null;
        }
        return { response: response, data: data };
    }

    _mutationOptions(method) {
        return {
            method: method,
            headers: {
                "Accept": "application/json",
                "X-NC-Auth": "1",
            },
        };
    }

    async _notifyIdentityChange(payload) {
        try {
            await Promise.resolve(this.onIdentityChange(payload));
        } catch (_error) {
            // The auth/history state is already committed; keep its UI usable.
        }
    }

    async initialize() {
        return this._refreshAuth(true);
    }

    async retry() {
        return this._refreshAuth(true);
    }

    async _refreshAuth(checkSession) {
        const generation = ++this.authGeneration;
        try {
            const result = await this._fetchJson("/api/auth/config");
            if (this.destroyed || generation !== this.authGeneration) return;
            const clientId = stringValue(result.data && result.data.clientId).trim();
            if (!result.response.ok) {
                this._showLoginUnavailable("LOGIN UNAVAILABLE");
                return;
            }
            if (!clientId || !result.data || result.data.mode === "off") {
                this._showAuthOff();
                return;
            }
            this.config = {
                clientId: clientId,
                mode: stringValue(result.data.mode),
                nonce: stringValue(result.data.nonce),
            };
            this._enableAuthSurface();
            if (checkSession) {
                const session = await this._fetchJson("/api/me");
                if (this.destroyed || generation !== this.authGeneration) return;
                if (session.response.ok) {
                    const user = this._mergeCachedUser(normalizedUser(session.data && session.data.user));
                    if (!user) {
                        this._showLoginUnavailable("LOGIN UNAVAILABLE");
                        return;
                    }
                    this._showSignedIn(user);
                    await this.loadConversations(false);
                    return;
                }
                if (session.response.status !== 401) {
                    this._showLoginUnavailable("LOGIN UNAVAILABLE");
                    return;
                }
            }
            this._showSignedOutHistory(
                "Sign in to save and revisit conversations. Live voice works without an account.",
            );
            await this._renderGoogleButton();
        } catch (_error) {
            if (!this.destroyed && generation === this.authGeneration) {
                this._showLoginUnavailable("LOGIN UNAVAILABLE");
            }
        }
    }

    _enableAuthSurface() {
        this._element("header-channel").hidden = true;
        this._element("auth-root").hidden = false;
        this._element("past-conversations").hidden = false;
    }

    _showAuthOff() {
        this.config = null;
        this.user = null;
        this.gisGeneration += 1;
        this._clearGisRenderTimer();
        this._closeMenu();
        this.closeConversation();
        this._element("auth-root").hidden = true;
        this._element("header-channel").hidden = false;
        this._element("past-conversations").hidden = true;
    }

    _showLoginUnavailable(message) {
        this.user = null;
        this._enableAuthSurface();
        this.gisGeneration += 1;
        this._clearGisRenderTimer();
        this._closeMenu();
        this._element("google-signin-button").hidden = true;
        this._element("auth-user-button").hidden = true;
        this._element("auth-status").textContent = message;
        this._element("auth-status-wrap").hidden = false;
        this._element("auth-retry-button").hidden = false;
        this._showSignedOutHistory(
            "Login unavailable. Live voice sessions are still available.",
        );
    }

    _showSignedOutHistory(message) {
        this.historyGeneration += 1;
        this.detailGeneration += 1;
        this.detailConversationId = null;
        this.historyCursor = null;
        this.turnCursor = null;
        this.historyCount = 0;
        this.closeConversation();
        this._element("history-list").replaceChildren();
        this._element("history-status").textContent = message;
        this._element("history-status").hidden = false;
        this._element("history-more-button").hidden = true;
        this._element("history-refresh-button").disabled = true;
    }

    _showSignedIn(user) {
        this.user = user;
        this.gisGeneration += 1;
        this._clearGisRenderTimer();
        this._enableAuthSurface();
        this._element("google-signin-button").hidden = true;
        this._element("auth-status-wrap").hidden = true;
        this._element("auth-retry-button").hidden = true;
        renderIdentity(
            this._element("auth-user-name"),
            this._element("auth-avatar"),
            user,
        );
        this._element("auth-user-button").hidden = false;
        this._element("history-refresh-button").disabled = false;
        this._writeCachedUser(user);
    }

    _showSignedOutHeader() {
        this.user = null;
        this._closeMenu();
        this._element("auth-user-button").hidden = true;
        this._element("auth-status-wrap").hidden = true;
        this._element("auth-retry-button").hidden = true;
        this._element("google-signin-button").hidden = false;
    }

    async _renderGoogleButton() {
        if (!this.config || !this.config.clientId) return;
        const generation = ++this.gisGeneration;
        this._showSignedOutHeader();
        const mount = this._element("google-signin-button");
        mount.replaceChildren();
        mount.setAttribute("aria-busy", "true");
        this._element("auth-status").textContent = "SIGN-IN LOADING";
        this._element("auth-status-wrap").hidden = false;
        this._element("auth-retry-button").hidden = true;
        try {
            const googleIdentity = await loadGoogleIdentityScript({
                document: this.document,
                window: this.window,
                timeoutMs: this.gisTimeoutMs,
                setTimeout: this.setTimeout,
                clearTimeout: this.clearTimeout,
            });
            if (this.destroyed || generation !== this.gisGeneration || this.user) return;
            googleIdentity.initialize({
                client_id: this.config.clientId,
                callback: this.handleCredentialResponse.bind(this),
                nonce: this.config.nonce,
                auto_select: false,
                ux_mode: "popup",
            });
            googleIdentity.renderButton(mount, {
                // GIS does not personalize medium buttons or widths below
                // 200px, so this control cannot turn into a profile-image UI.
                type: "standard",
                theme: "outline",
                size: "medium",
                text: "signin_with",
                shape: "rectangular",
                logo_alignment: "left",
                width: 184,
            });
            mount.removeAttribute("aria-busy");
            this._element("auth-status-wrap").hidden = true;
            this._clearGisRenderTimer();
            this.gisRenderTimer = this.setTimeout(function () {
                this.gisRenderTimer = null;
                if (
                    generation === this.gisGeneration
                    && !this.user
                    && !mount.firstChild
                ) {
                    this._showLoginUnavailable("LOGIN UNAVAILABLE");
                }
            }.bind(this), 1500);
        } catch (_error) {
            if (!this.destroyed && generation === this.gisGeneration && !this.user) {
                mount.removeAttribute("aria-busy");
                this._showLoginUnavailable("LOGIN UNAVAILABLE");
            }
        }
    }

    async handleCredentialResponse(value) {
        const credential = stringValue(value && value.credential);
        if (!credential || this.signInBusy || !this.config) {
            if (!this.signInBusy) this._showLoginUnavailable("LOGIN UNAVAILABLE");
            return;
        }
        this.signInBusy = true;
        this._clearGisRenderTimer();
        this._element("google-signin-button").setAttribute("aria-busy", "true");
        this._element("auth-status").textContent = "SIGNING IN";
        this._element("auth-status-wrap").hidden = false;
        try {
            const result = await this._fetchJson("/api/auth/google", {
                method: "POST",
                headers: {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-NC-Auth": "1",
                },
                body: JSON.stringify({ credential: credential }),
            });
            const user = normalizedUser(result.data && result.data.user);
            if (!result.response.ok || !user) {
                this._showLoginUnavailable("LOGIN UNAVAILABLE");
                return;
            }
            this._showSignedIn(user);
            await this._notifyIdentityChange({ reason: "login", signedIn: true });
            await this.loadConversations(false);
        } catch (_error) {
            this._showLoginUnavailable("LOGIN UNAVAILABLE");
        } finally {
            this.signInBusy = false;
            this._element("google-signin-button").removeAttribute("aria-busy");
        }
    }

    _readCachedUser() {
        try {
            return normalizedUser(JSON.parse(
                this.window.sessionStorage.getItem(DISPLAY_CACHE_KEY) || "null",
            ));
        } catch (_error) {
            return null;
        }
    }

    _mergeCachedUser(user) {
        if (!user) return null;
        const cached = this._readCachedUser();
        if (!cached || cached.sub !== user.sub) return user;
        return Object.assign({}, user, {
            name: user.name || cached.name,
            email: user.email || cached.email,
        });
    }

    _writeCachedUser(user) {
        try {
            this.window.sessionStorage.setItem(DISPLAY_CACHE_KEY, JSON.stringify({
                sub: user.sub,
                tenant: user.tenant,
                name: user.name,
                email: user.email,
            }));
        } catch (_error) {
            // Private browsing can disable storage; the signed-in UI still works.
        }
    }

    _clearCachedUser() {
        try {
            this.window.sessionStorage.removeItem(DISPLAY_CACHE_KEY);
        } catch (_error) {
            // Nothing else depends on display-claim caching.
        }
    }

    _toggleMenu() {
        const menu = this._element("auth-menu");
        const opening = menu.hidden;
        menu.hidden = !opening;
        this._element("auth-user-button").setAttribute("aria-expanded", String(opening));
    }

    _closeMenu() {
        this._element("auth-menu").hidden = true;
        this._element("auth-user-button").setAttribute("aria-expanded", "false");
    }

    _handleDocumentClick(event) {
        if (!this._element("auth-root").contains(event.target)) this._closeMenu();
    }

    _handleDocumentKeydown(event) {
        if (event.key !== "Escape") return;
        if (!this._element("auth-menu").hidden) {
            this._closeMenu();
            this._element("auth-user-button").focus();
        } else if (this.detailConversationId) {
            this.closeConversation();
        }
    }

    _openHistoryFromMenu() {
        this._closeMenu();
        this.closeConversation();
        const panel = this._element("past-conversations");
        if (typeof panel.scrollIntoView === "function") {
            panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
        this._element("history-heading").focus();
        this.loadConversations(false);
    }

    async signOut() {
        if (!this.user) return;
        const button = this._element("auth-signout-button");
        button.disabled = true;
        try {
            const result = await this._fetchJson(
                "/api/auth/logout",
                this._mutationOptions("POST"),
            );
            if (!result.response.ok && result.response.status !== 503) {
                this._element("history-status").textContent = "Could not sign out. Try again.";
                this._element("history-status").hidden = false;
                return;
            }
            this._clearCachedUser();
            this.user = null;
            this._showSignedOutHistory("Signed out. Live sessions remain available.");
            await this._notifyIdentityChange({ reason: "logout", signedIn: false });
            await this._refreshAuth(false);
        } catch (_error) {
            this._element("history-status").textContent = "Could not sign out. Try again.";
            this._element("history-status").hidden = false;
        } finally {
            button.disabled = false;
        }
    }

    async loadConversations(append) {
        if (!this.user) return;
        const generation = ++this.historyGeneration;
        const list = this._element("history-list");
        const status = this._element("history-status");
        const moreButton = this._element("history-more-button");
        if (!append) {
            this.historyCursor = null;
            this.historyCount = 0;
            list.replaceChildren();
            status.textContent = "Loading saved conversations…";
            status.hidden = false;
        }
        moreButton.disabled = true;
        try {
            const query = new URLSearchParams({ limit: String(DEFAULT_PAGE_SIZE) });
            if (append && this.historyCursor) query.set("cursor", this.historyCursor);
            const result = await this._fetchJson("/api/conversations?" + query.toString());
            if (this.destroyed || generation !== this.historyGeneration || !this.user) return;
            if (result.response.status === 401) {
                await this._sessionEnded();
                return;
            }
            if (!result.response.ok || !result.data || !Array.isArray(result.data.conversations)) {
                status.textContent = "History unavailable. Live conversation is unaffected.";
                status.hidden = false;
                return;
            }
            for (const conversation of result.data.conversations) {
                if (!conversation || typeof conversation.id !== "string") continue;
                list.appendChild(createConversationListItem(
                    this.document,
                    conversation,
                    {
                        onOpen: this.openConversation.bind(this),
                        onDelete: this.deleteConversation.bind(this),
                    },
                    { now: this.now(), locale: this.locale },
                ));
                this.historyCount += 1;
            }
            this.historyCursor = typeof result.data.nextCursor === "string"
                ? result.data.nextCursor
                : null;
            moreButton.hidden = !this.historyCursor;
            status.textContent = this.historyCount
                ? ""
                : "No saved conversations yet.";
            status.hidden = this.historyCount > 0;
        } catch (_error) {
            if (generation === this.historyGeneration) {
                status.textContent = "History unavailable. Live conversation is unaffected.";
                status.hidden = false;
            }
        } finally {
            if (generation === this.historyGeneration) moreButton.disabled = false;
        }
    }

    notifyHistoryChanged() {
        if (!this.user || this.detailConversationId || this.destroyed) return;
        if (this.historyRefreshTimer !== null) this.clearTimeout(this.historyRefreshTimer);
        this.historyRefreshTimer = this.setTimeout(function () {
            this.historyRefreshTimer = null;
            this.loadConversations(false);
        }.bind(this), 700);
    }

    async openConversation(conversation) {
        if (!this.user || !conversation || typeof conversation.id !== "string") return;
        this.detailConversationId = conversation.id;
        this.turnCursor = null;
        this._element("transcription-panel").classList.add("history-reading");
        this._element("transcription-eyebrow").textContent = "HISTORY";
        this._element("transcription-heading").textContent = "Saved transcript";
        this._element("transcription-live").hidden = true;
        this._element("history-list-view").hidden = true;
        this._element("history-detail-view").hidden = false;
        this._element("history-detail-title").textContent = stringValue(conversation.title).trim()
            || "Untitled conversation";
        this._element("history-detail-meta").textContent = formatRelativeTime(
            conversation.startedAt,
            this.now(),
            this.locale,
        );
        this._element("history-transcript").replaceChildren();
        this._element("history-detail-status").textContent = "Loading transcript…";
        this._element("history-detail-status").hidden = false;
        this._element("history-turns-more-button").hidden = true;
        await this.loadConversationTurns(false);
    }

    closeConversation() {
        this.detailGeneration += 1;
        this.detailConversationId = null;
        this.turnCursor = null;
        this._element("transcription-panel").classList.remove("history-reading");
        this._element("transcription-eyebrow").textContent = "LIVE SESSION";
        this._element("transcription-heading").textContent = "Transcription";
        this._element("transcription-live").hidden = false;
        this._element("history-list-view").hidden = false;
        this._element("history-detail-view").hidden = true;
    }

    async loadConversationTurns(append) {
        const conversationId = this.detailConversationId;
        if (!this.user || !conversationId) return;
        const generation = ++this.detailGeneration;
        const transcript = this._element("history-transcript");
        const status = this._element("history-detail-status");
        const moreButton = this._element("history-turns-more-button");
        if (!append) {
            this.turnCursor = null;
            transcript.replaceChildren();
        }
        moreButton.disabled = true;
        try {
            const query = new URLSearchParams({ limit: String(DEFAULT_TURN_PAGE_SIZE) });
            if (append && this.turnCursor) query.set("cursor", this.turnCursor);
            const result = await this._fetchJson(
                "/api/conversations/" + encodeURIComponent(conversationId) + "?" + query.toString(),
            );
            if (
                this.destroyed
                || generation !== this.detailGeneration
                || conversationId !== this.detailConversationId
            ) return;
            if (result.response.status === 401) {
                await this._sessionEnded();
                return;
            }
            if (!result.response.ok || !result.data || !Array.isArray(result.data.turns)) {
                status.textContent = result.response.status === 404
                    ? "This conversation no longer exists."
                    : "Transcript unavailable. Live conversation is unaffected.";
                status.hidden = false;
                return;
            }
            const detail = result.data.conversation;
            if (detail && typeof detail === "object") {
                this._element("history-detail-title").textContent = stringValue(detail.title).trim()
                    || "Untitled conversation";
                this._element("history-detail-meta").textContent = formatRelativeTime(
                    detail.startedAt,
                    this.now(),
                    this.locale,
                );
            }
            for (const turn of result.data.turns) {
                transcript.appendChild(createHistoryTurn(
                    this.document,
                    turn,
                    { locale: this.locale },
                ));
            }
            this.turnCursor = typeof result.data.nextCursor === "string"
                ? result.data.nextCursor
                : null;
            moreButton.hidden = !this.turnCursor;
            status.textContent = transcript.children.length ? "" : "This conversation has no saved turns.";
            status.hidden = transcript.children.length > 0;
        } catch (_error) {
            if (generation === this.detailGeneration) {
                status.textContent = "Transcript unavailable. Live conversation is unaffected.";
                status.hidden = false;
            }
        } finally {
            if (generation === this.detailGeneration) moreButton.disabled = false;
        }
    }

    async deleteConversation(conversation, button) {
        if (!this.user || !conversation || typeof conversation.id !== "string") return;
        if (
            typeof this.window.confirm === "function"
            && !this.window.confirm("Delete this saved conversation? This cannot be undone.")
        ) return;
        button.disabled = true;
        let shouldReconnect = false;
        try {
            const result = await this._fetchJson(
                "/api/conversations/" + encodeURIComponent(conversation.id),
                this._mutationOptions("DELETE"),
            );
            if (result.response.status === 401) {
                await this._sessionEnded();
                return;
            }
            if (!result.response.ok) {
                this._element("history-status").textContent = "Could not delete the conversation.";
                this._element("history-status").hidden = false;
                shouldReconnect = result.response.status >= 500;
                return;
            }
            shouldReconnect = true;
            await this.loadConversations(false);
        } catch (_error) {
            this._element("history-status").textContent = "Could not delete the conversation.";
            this._element("history-status").hidden = false;
        } finally {
            button.disabled = false;
            if (shouldReconnect) {
                await this._notifyIdentityChange({
                    reason: "history-delete",
                    signedIn: true,
                });
            }
        }
    }

    async deleteAllHistory() {
        if (!this.user) return;
        if (
            typeof this.window.confirm === "function"
            && !this.window.confirm("Delete all saved conversation history? This cannot be undone.")
        ) return;
        const button = this._element("auth-delete-all-button");
        button.disabled = true;
        let shouldReconnect = false;
        try {
            const result = await this._fetchJson(
                "/api/conversations",
                this._mutationOptions("DELETE"),
            );
            if (result.response.status === 401) {
                await this._sessionEnded();
                return;
            }
            if (!result.response.ok) {
                this._element("history-status").textContent = "Could not delete history.";
                this._element("history-status").hidden = false;
                shouldReconnect = result.response.status >= 500;
                return;
            }
            shouldReconnect = true;
            this._closeMenu();
            this.closeConversation();
            this.historyCursor = null;
            this.historyCount = 0;
            this._element("history-list").replaceChildren();
            this._element("history-status").textContent = "No saved conversations yet.";
            this._element("history-status").hidden = false;
            this._element("history-more-button").hidden = true;
        } catch (_error) {
            this._element("history-status").textContent = "Could not delete history.";
            this._element("history-status").hidden = false;
        } finally {
            button.disabled = false;
            if (shouldReconnect) {
                await this._notifyIdentityChange({
                    reason: "history-delete-all",
                    signedIn: true,
                });
            }
        }
    }

    async _sessionEnded() {
        this._clearCachedUser();
        this.user = null;
        this._showSignedOutHistory("Your session ended. Sign in to view saved history.");
        await this._notifyIdentityChange({ reason: "session-ended", signedIn: false });
        await this._refreshAuth(false);
    }

    _clearGisRenderTimer() {
        if (this.gisRenderTimer === null) return;
        this.clearTimeout(this.gisRenderTimer);
        this.gisRenderTimer = null;
    }

    destroy() {
        this.destroyed = true;
        this.authGeneration += 1;
        this.gisGeneration += 1;
        this.historyGeneration += 1;
        this.detailGeneration += 1;
        this._clearGisRenderTimer();
        if (this.historyRefreshTimer !== null) {
            this.clearTimeout(this.historyRefreshTimer);
            this.historyRefreshTimer = null;
        }
        this.document.removeEventListener("click", this._boundDocumentClick);
        this.document.removeEventListener("keydown", this._boundDocumentKeydown);
    }
}

export function createAuthHistoryUI(options) {
    return new AuthHistoryUI(options);
}
