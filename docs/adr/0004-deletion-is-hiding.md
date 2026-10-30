# Deletion is hiding, in both directions

Deleting an Event hides its Record (`HIDDEN` prefix); hiding a Record deletes its Event. The bridge keeps a Tombstone so un-hiding the Record recreates the Event with the same identity, and TimeTagger's own convention keeps the Record recoverable in the other direction.

TimeTagger cannot hard-delete, so "delete" always means hide there. Mirroring that asymmetry into the calendar is the only way to make deletion reversible on both sides and to keep one mis-tap in Apple Calendar from destroying a tracked interval. Deleting a whole calendar is refused and logged, since it would mass-hide records.
