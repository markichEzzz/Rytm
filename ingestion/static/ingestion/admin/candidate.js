(function () {
  'use strict';
  function init() {
    const priceStatus = document.getElementById('id_price_status');
    const price = document.getElementById('id_price');
    if (priceStatus && price) {
      const priceRow = price.closest('.form-row');
      const togglePrice = () => {
        const paid = priceStatus.value === 'paid';
        if (priceRow) priceRow.hidden = !paid;
        price.disabled = !paid;
        price.required = paid;
      };
      priceStatus.addEventListener('change', togglePrice);
      togglePrice();
    }
    const host = document.getElementById('candidate-location-map');
    const lat = document.getElementById('id_latitude');
    const lon = document.getElementById('id_longitude');
    if (host && window.L) {
      const pair = () => {
        const a = lat ? lat.value : host.dataset.latitude;
        const b = lon ? lon.value : host.dataset.longitude;
        if (!a.trim() || !b.trim()) return null;
        const p = [Number(a), Number(b)];
        return p.every(Number.isFinite) && Math.abs(p[0]) <= 90 && Math.abs(p[1]) <= 180 ? p : null;
      };
      const current = pair();
      const map = L.map(host, {scrollWheelZoom: false}).setView(current || [49.8397, 24.0297], current ? 16 : 13);
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        // Send only the site's origin: OSM requires a Referer, while Django's
        // default same-origin policy suppresses it on these external images.
        referrerPolicy: 'strict-origin-when-cross-origin',
        maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>'
      }).addTo(map);
      let marker;
      const icon = L.divIcon({className: 'candidate-map-marker', html: '<span></span>', iconSize: [24, 24], iconAnchor: [12, 12]});
      function select(p, write) {
        if (!marker) {
          marker = L.marker(p, {draggable: Boolean(lat && lon), icon}).addTo(map);
          marker.on('dragend', () => select([marker.getLatLng().lat, marker.getLatLng().lng], true));
        } else marker.setLatLng(p);
        if (write && lat && lon) {
          lat.value = p[0].toFixed(6); lon.value = p[1].toFixed(6);
          lat.dispatchEvent(new Event('input', {bubbles: true}));
          lon.dispatchEvent(new Event('input', {bubbles: true}));
        }
      }
      if (current) select(current, false);
      if (lat && lon) map.on('click', e => select([e.latlng.lat, e.latlng.lng], true));
      [lat, lon].filter(Boolean).forEach(field => field.addEventListener('change', () => {
        const p = pair();
        if (p) {select(p, false); map.panTo(p);}
        else if (marker) {map.removeLayer(marker); marker = null;}
      }));
    } else if (host) host.textContent = 'Map unavailable. Enter coordinates below.';
    document.querySelectorAll('.candidate-source-image').forEach(img => {
      const unavailable = () => {img.hidden = true; img.closest('.source-image-preview').querySelector('.source-image-error').hidden = false;};
      img.addEventListener('error', unavailable);
      if (img.complete && !img.naturalWidth) unavailable();
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
