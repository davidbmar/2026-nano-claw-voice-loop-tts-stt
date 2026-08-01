/* Phone-number banner: shows the DID currently pointed at this node.
 * Driven by NANO_CLAW_PHONE_DISPLAY_NUMBER via /api/phone/config so the
 * page always matches the live routing instead of a hardcoded number.
 * Hidden when the phone gateway is off or no display number is set. */
(function () {
  "use strict";
  fetch("/api/phone/config")
    .then(function (resp) { return resp.ok ? resp.json() : null; })
    .then(function (cfg) {
      var display =
        cfg && typeof cfg.display_number === "string"
          ? cfg.display_number.trim()
          : "";
      if (!display) return;
      var banner = document.getElementById("phone-banner");
      var number = document.getElementById("phone-banner-number");
      if (!banner || !number) return;
      number.textContent = display;
      var digits = display.replace(/[^0-9+]/g, "");
      if (digits) {
        number.href = "tel:" + (digits.charAt(0) === "+" ? digits : "+1" + digits);
      }
      banner.hidden = false;
    })
    .catch(function () {
      /* banner is decorative; a failed fetch just leaves it hidden */
    });
})();
