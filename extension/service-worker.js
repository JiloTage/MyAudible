// Opening through the extension action grants activeTab for that tab.
chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({openPanelOnActionClick: true}).catch(console.error);
});
chrome.runtime.onStartup.addListener(() => {
  chrome.sidePanel.setPanelBehavior({openPanelOnActionClick: true}).catch(console.error);
});
chrome.sidePanel.setPanelBehavior({openPanelOnActionClick: true}).catch(console.error);
