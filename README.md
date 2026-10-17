# Wood & String

*A local music player, and my experiment in learning songs through guitar.*

I’m into guitars. Radiohead’s *Weird Fishes / Arpeggi* and Dahi’s *How To Pray* made me want to study the parts I kept coming back to, then hear my own instrumental renditions on acoustic guitar, electric guitar and bass.

Wood & String is what I’m building toward that idea: keep the original recording, understand what matters in it, and turn those ideas into playable parts. Local processing and offline listening are the aim.

> **Development snapshot:** this is an unfinished prototype with source and tests, not a ready-to-install release or a reliable automatic song arranger.

## What’s here

| Area | Current prototype |
| --- | --- |
| Local music | Import and play a library, download music for offline listening, and queue short preview requests for the laptop. |
| Short previews | Experimental acoustic and Jazz Bass previews, with candidate notes that can be inspected and revised. |
| Acoustic parts | Explicit Load, Play, mute, All and seek controls. The new source remembers a listening mix on the same device. |
| Musical analysis | Experimental phrase and focus tools that keep competing note interpretations and uncertainty visible. |

The new mix storage and playback logic pass silent automated checks. Browser playback, phone setup, cross-device behavior and listening acceptance still need testing. A selected-note acoustic worker saved a part without changing its master audio, but its full test remains failed because the final checker rejected generated methods. The [worker notes](docs/ACOUSTIC-COMPANION-WORKER-001.md) explain that distinction.

**Still unfinished:** automatic transcription accuracy, expressive arrangements, full songs, Electric, optional drums, Ensemble and the local AI arranger. Electric remains disabled in the app. The two songs above are inspirations and planned listening tests, not successful recreations.

## From a song to playable parts

![Short-preview flow: preserve the original, estimate notes, assign parts, render, compare and revise.](diagrams/how-a-preview-works.png)

*The short-preview workflow, drawn on 16 October 2026. Earlier choppy automatic previews remain rejected.*

![Composer plan: understand the source, choose musical roles, render instruments, add effects and evaluate the result.](diagrams/what-the-composer-adds.png)

*The composer I’m working toward. This map includes planned connections as well as existing pieces.*

## What I’m learning

Getting the pitches right isn’t enough. Notes need to ring, repeated attacks need to stay distinct, and a quiet passage shouldn’t lose its intention. Following the singer can also miss the guitar pattern that made me interested in a song in the first place.

That is why I’m keeping vocal removal separate from recreating a performance, preserving timing, and recording specific problems with sustain, attacks, rests, dynamics and tone. A passing test doesn’t establish that an arrangement sounds right.

I want three approaches: **Keep it close**, **Essentials** and **Reimagine**. Essentials would keep the main riff or melody and leave the extra layers out. These are planned arrangement choices. Likewise, muting one part of a Duo leaves a Duo mix; making a complete Solo takes a new musical decision.

Some of that work is documented in the [phrase reader](docs/INSTRUMENTAL-PHRASE-EVIDENCE-001.md) and [focus proposer](docs/FOCUS-POLICY-REPAIR-002.md). They are development tools, not proof of automatic musical accuracy. The next milestone is better musical decisions followed by real-song listening comparisons.

## About this repository

This public snapshot contains source and tests. Recordings, sample banks, model weights, downloaded runtimes and private development data are excluded. Setup and reproducibility instructions will follow once the app’s behavior is settled.

Original code uses the [MIT license](LICENSE). [Third-party code and notices](THIRD_PARTY.md) retain their own licenses.
