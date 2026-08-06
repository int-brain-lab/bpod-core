// Report documentation searches to Umami.
//
// Sphinx puts the query in the URL of the search page (?q=...) and renders the
// results asynchronously into #search-results. We therefore read the query from
// the URL and wait for the result list to stop changing before reporting, so the
// number of hits can be sent along -- searches with zero results are the ones
// worth acting on. No-op when analytics is not loaded.

(function () {
  'use strict';

  if (!/(^|\/)search\.html$/.test(window.location.pathname)) return;

  var query = new URLSearchParams(window.location.search).get('q');
  if (!query) return;

  var sent = false;

  function report(results) {
    if (sent) return;
    sent = true;
    if (!window.umami) return;
    var data = { query: query };
    if (results !== undefined) data.results = results;
    window.umami.track('search', data);
  }

  function countResults(container) {
    var list = container.querySelector('ul.search');
    return list ? list.children.length : 0;
  }

  window.addEventListener('DOMContentLoaded', function () {
    var container = document.getElementById('search-results');
    if (!container || !window.MutationObserver) {
      report();
      return;
    }

    // Report once the result list has been quiet for a moment. Sphinx renders one
    // hit every few milliseconds, so a short delay is enough to catch them all.
    var settle;
    var observer = new MutationObserver(function () {
      clearTimeout(settle);
      settle = setTimeout(function () {
        observer.disconnect();
        report(countResults(container));
      }, 500);
    });
    observer.observe(container, { childList: true, subtree: true });

    // Give up waiting if the search never settles.
    setTimeout(function () {
      observer.disconnect();
      report(countResults(container));
    }, 5000);

    // Flush early if the visitor clicks a hit before the delay elapses.
    window.addEventListener('pagehide', function () {
      observer.disconnect();
      report(countResults(container));
    });
  });
})();