# Wood & String

I’m into guitars, and songs like Radiohead’s *Weird Fishes / Arpeggi* and Dahi’s *How To Pray* made me want to hear and study music as instrumentals—and explore how those parts might translate to guitar and bass.

Wood & String is my unfinished experiment in a private, local music player that can help with that. I’m working through vocal separation, note transcription, playable arrangements and expressive rendering, with local processing and offline use as the aim.

## Where it is

The prototype stores and plays local music, makes short experimental acoustic and Jazz Bass previews, and lets me inspect candidate notes or revise a saved passage. Electric guitar, optional drums, full-song arrangements and a small local AI arranger are still being developed. This source snapshot is not a ready-to-install release.

## What I’m learning

The first previews were choppy. Getting pitches into a guitar sound wasn’t enough: ringing notes, repeated attacks, rests, phrasing and the identity of the important part all matter. Following the vocal melody can miss the guitar pattern that made me interested in the song in the first place.

That has shaped the engineering: keep originals intact, preserve source timing, show uncertain notes and omissions, and keep vocal removal separate from recreating a performance. A successful render or a passing test doesn’t mean the arrangement sounds right.

Next is making those musical decisions better, then returning to real-song listening comparisons. The two songs above are inspirations and planned tests, not examples I’ve already recreated successfully.

This repository contains source and tests. Recordings, sample banks, model weights, downloaded runtimes and private development data are not included. Third-party code and notices retain their own licenses.
