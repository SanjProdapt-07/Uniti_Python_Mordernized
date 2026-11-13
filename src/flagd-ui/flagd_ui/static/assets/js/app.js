// Copyright The OpenTelemetry Authors
// SPDX-License-Identifier: Apache-2.0

(() => {
  topbar.config({barColors: {0: "#29d"}, shadowColor: "rgba(0, 0, 0, .3)"})

  document.addEventListener("htmx:beforeRequest", (e) => {
    if (e.detail.boosted) topbar.show(300)
  })
  document.addEventListener("htmx:afterSettle", () => topbar.hide())
  document.addEventListener("htmx:responseError", () => topbar.hide())
  document.addEventListener("htmx:sendError", () => topbar.hide())

  document.addEventListener("click", (e) => {
    const flash = e.target.closest("[data-flash]")
    if (!flash) return
    flash.classList.add("transition-all", "ease-in", "duration-200")
    flash.classList.add("opacity-0", "translate-y-4", "sm:translate-y-0", "sm:scale-95")
  })

  document.addEventListener("htmx:wsOpen", () => {
    sessionStorage.removeItem("flagd-ui:reloaded")
    const fields = document.getElementById("scheduler-fields")
    if (fields) htmx.trigger(fields, "change")
  })

  document.addEventListener("htmx:wsClose", (e) => {
    if (e.detail.event.code === 1008 && !sessionStorage.getItem("flagd-ui:reloaded")) {
      sessionStorage.setItem("flagd-ui:reloaded", "1")
      window.location.reload()
    }
  })
})()
