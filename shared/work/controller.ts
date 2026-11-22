import { WorkClient } from "./client";
import {
  errorMessage,
  type Bucket,
  type TimelineEvent,
  type WorkSummary,
  type WorkView,
  type WorkFileKind,
} from "./types";

export interface WorkState {
  bucket: Bucket;
  items: WorkSummary[];
  cursor: string | null;
  more: boolean;
  selected: string;
  view: WorkView | null;
  timeline: TimelineEvent[];
  historyCursor: number;
  historyMore: boolean;
  inboxLoading: boolean;
  detailLoading: boolean;
  inboxError: string;
  detailError: string;
  busy: boolean;
  error: string;
}
export function mergeTimeline(
  a: TimelineEvent[],
  b: TimelineEvent[],
): TimelineEvent[] {
  return [...new Map([...a, ...b].map((e) => [e.sequence, e])).values()].sort(
    (x, y) => x.sequence - y.sequence,
  );
}
interface RefreshGeneration {
  pending: boolean;
  runner: Promise<void> | null;
  snapshot: number;
}

const newGeneration = (): RefreshGeneration => ({ pending: false, runner: null, snapshot: 0 });

/** Each refresh coalesces invalidations independently and applies only to its current generation. */
export class WorkController {
  state: WorkState = {
    bucket: "attention",
    items: [],
    cursor: null,
    more: false,
    selected: "",
    view: null,
    timeline: [],
    historyCursor: 0,
    historyMore: false,
    inboxLoading: false,
    detailLoading: false,
    inboxError: "",
    detailError: "",
    busy: false,
    error: "",
  };
  private listeners = new Set<() => void>();
  private inboxGeneration = newGeneration();
  private detailGeneration = newGeneration();
  private disposed = false;
  private seenChanges = new Map<
    string,
    { version: number; sequence: number }
  >();
  constructor(readonly api: WorkClient) {}
  start(): void {
    this.disposed = false;
  }
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  getSnapshot = (): WorkState => this.state;
  invalidate(data: Record<string, unknown>): void {
    const id = typeof data.task_id === "string" ? data.task_id : "";
    const version = Number(data.task_version),
      sequence = Number(data.event_sequence);
    if (
      !id ||
      !Number.isSafeInteger(version) ||
      !Number.isSafeInteger(sequence)
    )
      return;
    const previous = this.seenChanges.get(id);
    if (
      previous &&
      previous.version >= version &&
      previous.sequence >= sequence
    )
      return;
    this.seenChanges.delete(id);
    this.seenChanges.set(id, {
      version: Math.max(version, previous?.version || 0),
      sequence: Math.max(sequence, previous?.sequence || 0),
    });
    if (this.seenChanges.size > 512)
      this.seenChanges.delete(this.seenChanges.keys().next().value!);
    void this.refresh();
  }
  private set(patch: Partial<WorkState>): void {
    if (!this.disposed) {
      this.state = { ...this.state, ...patch };
      this.listeners.forEach((f) => f());
    }
  }
  select(id: string): void {
    this.detailGeneration = newGeneration();
    this.set({
      selected: id,
      view: null,
      timeline: [],
      historyCursor: 0,
      historyMore: false,
      detailLoading: Boolean(id),
      detailError: "",
      error: "",
    });
    void this.refresh();
  }
  bucket(bucket: Bucket): void {
    this.inboxGeneration = newGeneration();
    this.set({ bucket, items: [], cursor: null, more: false, inboxError: "", inboxLoading: true });
    void this.refreshInbox();
  }
  refresh = async (): Promise<void> => {
    if (this.disposed) return;
    await Promise.all([this.refreshInbox(), this.refreshDetail()]);
  };
  private enqueue(
    generation: RefreshGeneration,
    current: () => boolean,
    read: () => Promise<void>,
  ): Promise<void> {
    generation.pending = true;
    generation.runner ??= Promise.resolve().then(async () => {
      try {
        while (generation.pending && current()) {
          generation.pending = false;
          generation.snapshot++;
          await read();
        }
      } finally {
        generation.runner = null;
      }
    });
    return generation.runner;
  }
  private refreshInbox(): Promise<void> {
    const generation = this.inboxGeneration;
    const bucket = this.state.bucket;
    const current = () => !this.disposed && generation === this.inboxGeneration;
    return this.enqueue(generation, current, async () => {
      this.set({ inboxLoading: true });
      try {
        const inbox = await this.api.inbox(bucket);
        if (current()) this.set({
          items: inbox.items, cursor: inbox.next_cursor, more: inbox.has_more,
          inboxLoading: false, inboxError: "",
        });
      } catch (error) {
        if (current()) this.set({
          items: [], cursor: null, more: false, inboxLoading: false, inboxError: errorMessage(error),
        });
      }
    });
  }
  private refreshDetail(): Promise<void> {
    const generation = this.detailGeneration;
    const selected = this.state.selected;
    if (!selected) return Promise.resolve();
    const current = () => !this.disposed && generation === this.detailGeneration;
    return this.enqueue(generation, current, async () => {
      this.set({ detailLoading: true });
      let view: WorkView;
      try {
        view = await this.api.view(selected);
      } catch (error) {
        if (current()) this.set({
          view: null, timeline: [], historyCursor: 0, historyMore: false,
          detailLoading: false, detailError: errorMessage(error),
        });
        return;
      }
      if (!current()) return;
      const previous = this.state.view;
      const sameVisibility = view.visibility_revision !== undefined
        && previous?.task.task_id === view.task.task_id
        && previous.visibility_revision === view.visibility_revision;
      const first = view.timeline[0]?.sequence;
      const olderLoaded = sameVisibility && first !== undefined
        && this.state.timeline.some((row) => row.sequence === first)
        && this.state.timeline[0]?.sequence < first;
      if (previous && sameVisibility) {
        for (const kind of ["evidence", "artifacts"] as const) {
          if (previous[kind].length > 50) {
            view[kind] = [...new Map([...view[kind], ...previous[kind]].map((file) => [file.id, file])).values()];
            view.metadata_cursors = { ...view.metadata_cursors, [kind]: previous.metadata_cursors?.[kind] };
          }
        }
      }
      this.set({
        view, timeline: olderLoaded ? mergeTimeline(this.state.timeline, view.timeline) : view.timeline,
        historyCursor: olderLoaded ? this.state.historyCursor : view.history_cursor || first || 0,
        historyMore: olderLoaded ? this.state.historyMore : Boolean(view.truncated.timeline),
        detailLoading: false, detailError: "",
      });
      let after = view.last_sequence;
      try {
        for (let page = 0; page < 5 && current(); page++) {
          const delta = await this.api.timeline(selected, after);
          if (!current()) break;
          this.set({ timeline: mergeTimeline(this.state.timeline, delta.items) });
          if (!delta.has_more || delta.next_sequence <= after) break;
          after = delta.next_sequence;
        }
      } catch (error) {
        if (current()) this.set({ detailError: errorMessage(error) });
      }
    });
  }
  async more(): Promise<void> {
    if (!this.state.cursor || this.state.inboxLoading) return;
    const generation = this.inboxGeneration,
      snapshot = generation.snapshot;
    this.set({ inboxLoading: true, inboxError: "" });
    try {
      const page = await this.api.inbox(this.state.bucket, this.state.cursor);
      if (generation === this.inboxGeneration && snapshot === generation.snapshot)
        this.set({
          items: [
            ...new Map(
              [...this.state.items, ...page.items].map((i) => [i.task_id, i]),
            ).values(),
          ],
          cursor: page.next_cursor,
          more: page.has_more,
        });
    } catch (error) {
      if (generation === this.inboxGeneration && snapshot === generation.snapshot && !this.disposed)
        this.set({ inboxError: errorMessage(error) });
    } finally {
      if (generation === this.inboxGeneration && snapshot === generation.snapshot)
        this.set({ inboxLoading: false });
    }
  }
  async history(): Promise<void> {
    if (!this.state.selected || !this.state.historyCursor || !this.state.historyMore || this.state.detailLoading) return;
    const generation = this.detailGeneration,
      snapshot = generation.snapshot;
    this.set({ detailLoading: true, detailError: "" });
    try {
      const page = await this.api.olderTimeline(
        this.state.selected,
        this.state.historyCursor,
      );
      if (generation === this.detailGeneration && snapshot === generation.snapshot)
        this.set({
          timeline: mergeTimeline(this.state.timeline, page.items),
          historyCursor: page.next_sequence,
          historyMore: page.has_more,
        });
    } catch (error) {
      if (generation === this.detailGeneration && snapshot === generation.snapshot && !this.disposed)
        this.set({ detailError: errorMessage(error) });
    } finally {
      if (generation === this.detailGeneration && snapshot === generation.snapshot)
        this.set({ detailLoading: false });
    }
  }
  async olderFiles(kind: WorkFileKind): Promise<void> {
    const { view, selected } = this.state;
    const cursor = view?.metadata_cursors?.[kind];
    if (!view || !cursor || !selected || this.state.detailLoading) return;
    const generation = this.detailGeneration, snapshot = generation.snapshot;
    this.set({ detailLoading: true, detailError: "" });
    try {
      const page = await this.api.files(selected, kind, cursor);
      if (generation !== this.detailGeneration || snapshot !== generation.snapshot || this.disposed) return;
      this.set({ view: {
        ...view,
        [kind]: [...new Map([...view[kind], ...page.items].map((file) => [file.id, file])).values()],
        metadata_cursors: { ...view.metadata_cursors, [kind]: page.next_cursor },
      } });
    } catch (error) {
      if (generation === this.detailGeneration && snapshot === generation.snapshot && !this.disposed)
        this.set({ detailError: errorMessage(error) });
    } finally {
      if (generation === this.detailGeneration && snapshot === generation.snapshot)
        this.set({ detailLoading: false });
    }
  }
  async act(operation: () => Promise<unknown>): Promise<boolean> {
    if (this.state.busy) return false;
    this.set({ busy: true, error: "" });
    let succeeded = false,
      failure = "";
    try {
      await operation();
      succeeded = true;
    } catch (error) {
      failure = errorMessage(error);
    }
    // Also reconcile conflicts and ambiguous network failures; never optimistically change task state.
    await this.refresh();
    this.set({ busy: false, ...(failure ? { error: failure } : {}) });
    return succeeded;
  }
  dispose(): void {
    this.disposed = true;
    this.inboxGeneration = newGeneration();
    this.detailGeneration = newGeneration();
    this.listeners.clear();
  }
}
