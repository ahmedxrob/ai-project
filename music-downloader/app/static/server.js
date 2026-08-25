const express = require('express');
const path = require('path');
const app = express();

// Standard Subsonic response wrapper
const subsonicEnvelope = (payload = {}) => ({
  "subsonic-response": {
    "status": "ok",
    "version": "1.16.1",
    "type": "CustomApp",
    "serverVersion": "1.0.0",
    ...payload
  }
});

// Endpoint 1: Health check / connection ping
app.all(['/rest/ping', '/rest/ping.view'], (req, res) => {
  res.json(subsonicEnvelope());
});

// Endpoint 2: License status (Amperfy validation)
app.all(['/rest/getLicense', '/rest/getLicense.view'], (req, res) => {
  res.json(subsonicEnvelope({ license: { valid: true } }));
});

// Endpoint 3: Mock artist directory
app.all(['/rest/getArtists', '/rest/getArtists.view'], (req, res) => {
  res.json(subsonicEnvelope({
    artists: {
      index: [{
        name: "T",
        artist: [{ id: "1", name: "Test Artist" }]
      }]
    }
  }));
});

// Endpoint 4: Audio file streaming
app.all(['/rest/stream', '/rest/stream.view'], (req, res) => {
  const trackId = req.query.id || 'sample';
  const filePath = path.join(__dirname, 'media', `${trackId}.mp3`);
  
  res.sendFile(filePath, (err) => {
    if (err) res.status(404).end();
  });
});

app.listen(3000, () => console.log('Subsonic API listening on port 3000'));
