// Open the side panel from the extension's toolbar icon.
chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({openPanelOnActionClick: true}).catch(console.error);
});
chrome.runtime.onStartup.addListener(() => {
  chrome.sidePanel.setPanelBehavior({openPanelOnActionClick: true}).catch(console.error);
});
chrome.sidePanel.setPanelBehavior({openPanelOnActionClick: true}).catch(console.error);
