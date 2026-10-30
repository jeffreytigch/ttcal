# An Event and a Record are the same thing

TimeTagger records are edited one at a time from the CLI, which makes recurring or bulk work painful. The obvious alternative — calendar events are *plans*, records are *facts*, linked but separate — was rejected: it doubles the bookkeeping and breaks "delete the event, delete the record". We mirror 1:1 instead: an Event *is* a Record, created in either place, edited in either place.

Consequence to live with: editing a past Event rewrites tracked history, and a future Event is a future Record that already counts in TimeTagger's totals. There is no "planned but not yet worked" state.
