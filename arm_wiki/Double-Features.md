# Double Features

Some discs carry more than one film: a "Double Feature", a "2-Movie Collection", or a disc labelled
`STUART_LITTLE_1_AND_2`. A.R.M. gives such a disc one job, with one row per film. After the rip, each
ripped title whose length fits a film's runtime is filed as that film. The first film goes in the job's
own `Title (Year)` folder and every other film gets its own folder beside it. Titles that no film claims
are extras of the first film, as they would be for a single film.

Runtimes do not always place the films. The provider's runtime can be off: TMDB gives The Secret Life of
Cats and The Secret Life of Dogs 46 minutes each, but their titles on the DVD run 52 and 50 minutes. And
films given the same runtime (the providers give whole minutes) cannot be told apart by length. A.R.M.
then files those films in disc order: the first of them gets the first title on the disc that could be
any of them, the next film the next such title, and so on. Films whose runtimes differ, even by a minute,
are still told apart by length, whatever order the disc has them in. For films that no title's length
fits, disc order is used only when:

- every one of them has a known runtime,
- exactly as many titles are left over as there are such films, counting the titles that run at least
  half the shortest of their runtimes, and
- each title is within 30% of its film's runtime.

Otherwise the first film falls back to the usual main-feature rule, and a later film is not filed: its
title ends up with the extras.

## Found automatically

A.R.M. treats a disc as a double feature when one of these names several films:

- the product title of the case barcode (`Stuart Little / Stuart Little 2 (DVD)`)
- the disc label (`STUART_LITTLE_1_AND_2`)
- the disc's own metadata

It searches for each film separately and needs at least two matches. If it finds them, the job page
shows a **films** row listing each film, its runtime and, after the rip, the title it was filed from.

## Marking a disc by hand

A disc with a generic label (`DVD_VIDEO`) or a barcode that names only one film is identified as a
single film. The second film is then filed as an extra. To fix this while the disc waits for manual
identification (`MANUAL_WAIT`), click **Double Feature** on the disc's card on the home page or on its
job page. The Title Search and Custom Title pages link there too.

On that page:

1. Search for the first film on the disc and click **Add as film 1**.
2. Search for the next film and add it, and so on for each film on the disc.
3. Use the arrow to change the order. The first film is filed in the job's folder.

Once two films have been added, the job takes the first film's title, year and IMDb id, as a title fix
would. This also ends the manual wait, so the rip starts. Films added after the rip has started are
still used, because the rip is matched to the films only when it is filed.

Each film's runtime is looked up from the metadata provider (TMDB or OMDb) when it is added. A film with
no known runtime cannot be matched to a ripped title, by length or by disc order. The first film then
falls back to the usual main-feature rule; a later film is not filed: its title ends up with the
extras, or is left out when `MAINFEATURE` is on (see below).

**Not a double feature** removes every film. The job keeps its current title.

A disc marked as a [bonus disc](Bonus-Discs) cannot also be a double feature: clear the bonus disc
choice first.

## Title fixes

Choosing a title with **Title Search** or **Custom Title** says the disc is that one film, so the job's
films are removed. The exception is a title that is the first film, which is the one the job is filed
as anyway. A finished job keeps its films, because they record where each film was filed.

## MAINFEATURE

With `MAINFEATURE: true`, A.R.M. rips only one title, so the other films of a double feature would never
be ripped. For a job that is a double feature when its rip starts, A.R.M. turns `MAINFEATURE` off for
that job only. Every title of at least `MINLENGTH` is ripped, and the films are filed as usual. The
titles that no film claims are left out, as `MAINFEATURE` would have left them out.

A disc marked as a double feature after its rip has started is ripped with the settings it started
with. Mark it during the manual wait.
