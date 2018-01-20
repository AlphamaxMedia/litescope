from litex.gen import *
from litex.gen.genlib.cdc import MultiReg, PulseSynchronizer

from litex.build.tools import write_to_file

from litex.soc.interconnect.csr import *
from litex.soc.cores.gpio import GPIOInOut
from litex.soc.interconnect import stream


class LiteScopeIO(Module, AutoCSR):
    def __init__(self, dw):
        self.dw = dw
        self.input = Signal(dw)
        self.output = Signal(dw)

        # # #

        self.submodules.gpio = GPIOInOut(self.input, self.output)

    def get_csrs(self):
        return self.gpio.get_csrs()


def core_layout(dw, hw=1):
    return [("data", dw), ("hit", hw)]


class FrontendTrigger(Module, AutoCSR):
    def __init__(self, dw, depth=16):
        self.sink = stream.Endpoint(core_layout(dw))
        self.source = stream.Endpoint(core_layout(dw))

        self.mem_flush = CSR()
        self.mem_write = CSR()
        self.mem_mask = CSRStorage(dw)
        self.mem_value = CSRStorage(dw)

        # # #

        # memory and configuration
        mem = stream.AsyncFIFO([("mask", dw), ("value", dw)], depth)
        mem = ClockDomainsRenamer({"write": "csr_sys", "read": "sys"})(mem)
        self.submodules += mem
        self.comb += [
            mem.sink.valid.eq(self.mem_write.re),
            mem.sink.mask.eq(self.mem_mask.storage),
            mem.sink.value.eq(self.mem_value.storage)
        ]

        # hit and memory read/flush
        hit = Signal()
        flush = PulseSynchronizer("csr_sys", "sys")
        flush_count = Signal(max=depth)
        self.submodules += flush
        self.comb += flush.i.eq(self.mem_flush.re)
        self.sync += [
            If(flush.o,
                flush_count.eq(depth - 1)
            ).Else(
                flush_count.eq(flush_count - 1)
            )
        ]
        self.comb += [
            mem.source.ready.eq(hit | (flush_count != 0)),
            hit.eq((self.sink.data & mem.source.mask) == mem.source.value)
        ]

        # output
        self.comb += [
            self.sink.connect(self.source),
            self.source.hit.eq(~mem.source.valid)
        ]


class FrontendSubSampler(Module, AutoCSR):
    def __init__(self, dw):
        self.sink = stream.Endpoint(core_layout(dw))
        self.source = stream.Endpoint(core_layout(dw))

        self.value = CSRStorage(16)

        # # #

        value = Signal(16)
        self.specials += MultiReg(self.value.storage, value)

        counter = Signal(16)
        done = Signal()

        self.sync += \
            If(self.source.ready,
                If(done,
                    counter.eq(0)
                ).Elif(self.sink.valid,
                    counter.eq(counter + 1)
                )
            )

        self.comb += [
            done.eq(counter == value),
            self.sink.connect(self.source, omit=set(["valid"])),
            self.source.valid.eq(self.sink.valid & done)
        ]


class AnalyzerMux(Module, AutoCSR):
    def __init__(self, dw, n):
        self.sinks = [stream.Endpoint(core_layout(dw)) for i in range(n)]
        self.source = stream.Endpoint(core_layout(dw))

        self.value = CSRStorage(bits_for(n))

        # # #

        cases = {}
        for i in range(n):
            cases[i] = self.sinks[i].connect(self.source)
        self.comb += Case(self.value.storage, cases)


class AnalyzerFrontend(Module, AutoCSR):
    def __init__(self, dw, cd_ratio):
        self.sink = stream.Endpoint(core_layout(dw))
        self.source = stream.Endpoint(core_layout(dw*cd_ratio))

        # # #

        self.submodules.buffer = stream.Buffer(core_layout(dw))
        self.submodules.trigger = FrontendTrigger(dw)
        self.submodules.subsampler = FrontendSubSampler(dw)
        self.submodules.converter = stream.StrideConverter(
                core_layout(dw, 1), core_layout(dw*cd_ratio, cd_ratio))
        self.submodules.fifo = ClockDomainsRenamer(
            {"write": "sys", "read": "csr_sys"})(
                stream.AsyncFIFO(core_layout(dw*cd_ratio, cd_ratio), 8))
        self.submodules.pipeline = stream.Pipeline(
            self.sink,
            self.buffer,
            self.trigger,
            self.subsampler,
            self.converter,
            self.fifo,
            self.source)


class AnalyzerStorage(Module, AutoCSR):
    def __init__(self, dw, depth, cd_ratio):
        self.sink = stream.Endpoint(core_layout(dw, cd_ratio))

        self.start = CSR()
        self.length = CSRStorage(bits_for(depth))
        self.offset = CSRStorage(bits_for(depth))

        self.idle = CSRStatus()
        self.wait = CSRStatus()
        self.run  = CSRStatus()

        self.mem_flush = CSR()
        self.mem_valid = CSRStatus()
        self.mem_ready = CSR()
        self.mem_data = CSRStatus(dw)

        # # #

        mem = stream.SyncFIFO([("data", dw)], depth//cd_ratio, buffered=True)
        self.submodules += ResetInserter()(mem)
        self.comb += mem.reset.eq(self.mem_flush.re)

        fsm = FSM(reset_state="IDLE")
        self.submodules += fsm

        fsm.act("IDLE",
            self.idle.status.eq(1),
            If(self.start.re,
                NextState("WAIT")
            ),
            self.sink.ready.eq(1),
            mem.source.ready.eq(self.mem_ready.re & self.mem_ready.r)
        )
        fsm.act("WAIT",
            self.wait.status.eq(1),
            self.sink.connect(mem.sink, omit=set(["hit"])),
            If(self.sink.valid & (self.sink.hit != 0),
                NextState("RUN")
            ),
            mem.source.ready.eq(mem.level >= self.offset.storage)
        )
        fsm.act("RUN",
            self.run.status.eq(1),
            self.sink.connect(mem.sink, omit=set(["hit"])),
            If(~mem.sink.ready | (mem.level >= self.length.storage),
                NextState("IDLE"),
                mem.source.ready.eq(1)
            )
        )
        self.comb += [
            self.mem_valid.status.eq(mem.source.valid),
            self.mem_data.status.eq(mem.source.data)
        ]


def _format_groups(groups):
    if not isinstance(groups, dict):
        groups = {0 : groups}
    new_groups = {}
    for n, signals in groups.items():
        if not isinstance(signals, list):
            signals = [signals]

        split_signals = []
        for s in signals:
            if isinstance(s, Record):
                split_signals.extend(s.flatten())
            else:
                split_signals.append(s)
        new_groups[n] = split_signals
    return new_groups


class LiteScopeAnalyzer(Module, AutoCSR):
    def __init__(self, groups, depth, cd="sys", cd_ratio=1):
        self.groups = _format_groups(groups)
        self.dw = max([sum([len(s) for s in g]) for g in self.groups.values()])

        self.depth = depth
        self.cd_ratio = cd_ratio

        # # #

        self.submodules.mux = AnalyzerMux(self.dw, len(self.groups))
        for i, signals in self.groups.items():
            self.comb += [
                self.mux.sinks[i].valid.eq(1),
                self.mux.sinks[i].data.eq(Cat(signals))
            ]
        self.submodules.frontend = ClockDomainsRenamer(
            {"sys": cd, "csr_sys": "sys"})(AnalyzerFrontend(self.dw, cd_ratio))
        self.submodules.storage = AnalyzerStorage(self.dw*cd_ratio, depth, cd_ratio)
        self.comb += [
            self.mux.source.connect(self.frontend.sink),
            self.frontend.source.connect(self.storage.sink)
        ]

    def export_csv(self, vns, filename):
        def format_line(*args):
            return ",".join(args) + "\n"
        r = format_line("config", "None", "dw", str(self.dw))
        r += format_line("config", "None", "depth", str(self.depth))
        r += format_line("config", "None", "cd_ratio", str(int(self.cd_ratio)))
        for i, signals in self.groups.items():
            for s in signals:
                r += format_line("signal", str(i), vns.get_name(s), str(len(s)))
        write_to_file(filename, r)
