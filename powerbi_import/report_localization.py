"""Offline report UI localization. Only explicitly authored UI is translated.

LocalizedText is deliberately opt-in: ordinary strings (including workbook names,
diagnostics and evidence) are never looked up or translated. Parameters are text,
not HTML. The browser uses textContent/setAttribute, never innerHTML.
"""
from __future__ import annotations

import html


class LocalizedText(str):
    en: str
    fr: str

    def __new__(cls, en: str, fr: str):
        value = super().__new__(cls, en)
        value.en, value.fr = en, fr
        return value


def text(en: str, fr: str) -> LocalizedText:
    return LocalizedText(en, fr)


def attributes(value: LocalizedText, target: str = "") -> str:
    """Attributes for a text node, title, aria-label, placeholder, or image alt."""
    suffix = "-" + target if target else ""
    return (f'data-i18n{suffix}-en="{html.escape(value.en, quote=True)}" '
            f'data-i18n{suffix}-fr="{html.escape(value.fr, quote=True)}"')


def render(value: str, default: str = "en") -> str:
    if not isinstance(value, LocalizedText):
        return html.escape(str(value), quote=True)
    return (f'<span {attributes(value)}>'
            f'{html.escape(value.fr if default == "fr" else value.en)}</span>')


def selector(default: str = "en") -> str:
    label = text("Report language", "Langue du rapport")
    accessible = text("Choose report language", "Choisir la langue du rapport")
    return (
        '<div class="report-language"><label for="report-language" '
        + attributes(label)
        + '>' + (label.fr if default == "fr" else label.en) + '</label> '
        '<select id="report-language" '
        + attributes(accessible, "aria-label")
        + f' aria-label="{accessible.fr if default == "fr" else accessible.en}">'
        f'<option value="fr" lang="fr"{" selected" if default == "fr" else ""}>Français</option>'
        f'<option value="en" lang="en"{" selected" if default != "fr" else ""}>English</option></select></div>'
    )


def script() -> str:
    """Public hooks: ReportI18n.setLanguage, .language, .format; reportlanguagechange."""
    return r"""
(function () {
    var language = document.documentElement.lang === 'fr' ? 'fr' : 'en';
    var key = 'pbi-report-language';
    function format(en, fr, values) {
        var template = language === 'fr' ? fr : en;
        return template.replace(/\{(\w+)\}/g, function (_, name) {
            return values && Object.prototype.hasOwnProperty.call(values, name)
                ? String(values[name]) : _;
        });
    }
    function setLanguage(next, remember) {
        if (next !== 'fr' && next !== 'en') return;
        language = next;
        document.documentElement.lang = next;
        ['', 'title', 'aria-label', 'placeholder', 'alt'].forEach(function (target) {
            var prefix = 'data-i18n' + (target ? '-' + target : '');
            document.querySelectorAll('[' + prefix + '-' + next + ']').forEach(function (node) {
                var value = node.getAttribute(prefix + '-' + next);
                if (target) node.setAttribute(target, value);
                else node.textContent = value;
            });
        });
        var select = document.getElementById('report-language');
        if (select) select.value = next;
        if (remember !== false) {
            try { localStorage.setItem(key, next); } catch (_) {}
        }
        document.dispatchEvent(new Event('reportlanguagechange'));
    }
    window.ReportI18n = {
        setLanguage: setLanguage, format: format,
        get language() { return language; }
    };
    function init() {
        var saved;
        try { saved = localStorage.getItem(key); } catch (_) {}
        var select = document.getElementById('report-language');
        if (select) select.addEventListener('change', function () { setLanguage(this.value); });
        setLanguage(saved === 'fr' || saved === 'en' ? saved : language, false);
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
"""
