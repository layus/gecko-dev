/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

//! Servo, the mighty web browser engine from the future.
//!
//! This is a very simple library that wires all of Servo's components
//! together as type `Servo`, along with a generic client
//! implementing the `WindowMethods` trait, to create a working web
//! browser.
//!
//! The `Servo` type is responsible for configuring a
//! `Constellation`, which does the heavy lifting of coordinating all
//! of Servo's internal subsystems, including the `ScriptThread` and the
//! `LayoutThread`, as well maintains the navigation context.
//!
//! `Servo` is fed events from a generic type that implements the
//! `WindowMethods` trait.

extern crate env_logger;
#[cfg(all(not(target_os = "windows"), not(target_os = "ios")))]
extern crate gaol;
extern crate gleam;
extern crate log;

pub extern crate bluetooth;
pub extern crate bluetooth_traits;
pub extern crate canvas;
pub extern crate canvas_traits;
pub extern crate compositing;
pub extern crate constellation;
pub extern crate debugger;
pub extern crate devtools;
pub extern crate devtools_traits;
pub extern crate euclid;
pub extern crate gfx;
pub extern crate ipc_channel;
pub extern crate layout_thread;
pub extern crate msg;
pub extern crate net;
pub extern crate net_traits;
pub extern crate profile;
pub extern crate profile_traits;
pub extern crate script;
pub extern crate script_traits;
pub extern crate script_layout_interface;
pub extern crate servo_config;
pub extern crate servo_geometry;
pub extern crate servo_url;
pub extern crate style;
pub extern crate style_traits;
pub extern crate webrender_api;
pub extern crate webvr;
pub extern crate webvr_traits;

#[cfg(feature = "webdriver")]
extern crate webdriver_server;

extern crate webrender;

#[cfg(feature = "webdriver")]
fn webdriver(port: u16, constellation: Sender<ConstellationMsg>) {
    webdriver_server::start_server(port, constellation);
}

#[cfg(not(feature = "webdriver"))]
fn webdriver(_port: u16, _constellation: Sender<ConstellationMsg>) { }

use bluetooth::BluetoothThreadFactory;
use bluetooth_traits::BluetoothRequest;
use canvas::gl_context::GLContextFactory;
use canvas::webgl_thread::WebGLThreads;
use compositing::IOCompositor;
use compositing::compositor_thread::{self, CompositorProxy, CompositorReceiver, InitialCompositorState};
use compositing::windowing::WindowEvent;
use compositing::windowing::WindowMethods;
use constellation::{Constellation, InitialConstellationState, UnprivilegedPipelineContent};
use constellation::{FromCompositorLogger, FromScriptLogger};
#[cfg(all(not(target_os = "windows"), not(target_os = "ios")))]
use constellation::content_process_sandbox_profile;
use env_logger::Logger as EnvLogger;
#[cfg(all(not(target_os = "windows"), not(target_os = "ios")))]
use gaol::sandbox::{ChildSandbox, ChildSandboxMethods};
use gfx::font_cache_thread::FontCacheThread;
use ipc_channel::ipc::{self, IpcSender};
use log::{Log, LogMetadata, LogRecord};
use net::resource_thread::new_resource_threads;
use net_traits::IpcSend;
use profile::mem as profile_mem;
use profile::time as profile_time;
use profile_traits::mem;
use profile_traits::time;
use script_traits::{ConstellationMsg, SWManagerSenders, ScriptToConstellationChan};
use servo_config::opts;
use servo_config::prefs::PREFS;
use servo_config::resource_files::resources_dir_path;
use std::borrow::Cow;
use std::cmp::max;
use std::path::PathBuf;
use std::rc::Rc;
use std::sync::mpsc::{Sender, channel};
use webrender::RendererKind;
use webvr::{WebVRThread, WebVRCompositorHandler};

pub use gleam::gl;
pub use servo_config as config;
pub use servo_url as url;
pub use msg::constellation_msg::TopLevelBrowsingContextId as BrowserId;

/// The in-process interface to Servo.
///
/// It does everything necessary to render the web, primarily
/// orchestrating the interaction between JavaScript, CSS layout,
/// rendering, and the client window.
///
/// Clients create a `Servo` instance for a given reference-counted type
/// implementing `WindowMethods`, which is the bridge to whatever
/// application Servo is embedded in. Clients then create an event
/// loop to pump messages between the embedding application and
/// various browser components.
pub struct Servo<Window: WindowMethods + 'static> {
    compositor: IOCompositor<Window>,
    constellation_chan: Sender<ConstellationMsg>,
}

impl<Window> Servo<Window> where Window: WindowMethods + 'static {
    pub fn new(window: Rc<Window>) -> Servo<Window> {
        // Global configuration options, parsed from the command line.
        let opts = opts::get();

        // Make sure the gl context is made current.
        window.prepare_for_composite(0, 0);

        // Get both endpoints of a special channel for communication between
        // the client window and the compositor. This channel is unique because
        // messages to client may need to pump a platform-specific event loop
        // to deliver the message.
        let (compositor_proxy, compositor_receiver) =
            create_compositor_channel(window.create_event_loop_waker());
        let supports_clipboard = window.supports_clipboard();
        let time_profiler_chan = profile_time::Profiler::create(&opts.time_profiling,
                                                                opts.time_profiler_trace_path.clone());
        let mem_profiler_chan = profile_mem::Profiler::create(opts.mem_profiler_period);
        let debugger_chan = opts.debugger_port.map(|port| {
            debugger::start_server(port)
        });
        let devtools_chan = opts.devtools_port.map(|port| {
            devtools::start_server(port)
        });

        let mut resource_path = resources_dir_path().unwrap();
        resource_path.push("shaders");

        let (mut webrender, webrender_api_sender) = {
            // TODO(gw): Duplicates device_pixels_per_screen_px from compositor. Tidy up!
            let scale_factor = window.hidpi_factor().get();
            let device_pixel_ratio = match opts.device_pixels_per_px {
                Some(device_pixels_per_px) => device_pixels_per_px,
                None => match opts.output_file {
                    Some(_) => 1.0,
                    None => scale_factor,
                }
            };

            let renderer_kind = if opts::get().should_use_osmesa() {
                RendererKind::OSMesa
            } else {
                RendererKind::Native
            };

            let recorder = if opts.webrender_record {
                let record_path = PathBuf::from("wr-record.bin");
                let recorder = Box::new(webrender::BinaryRecorder::new(&record_path));
                Some(recorder as Box<webrender::ApiRecordingReceiver>)
            } else {
                None
            };

            let mut debug_flags = webrender::DebugFlags::empty();
            debug_flags.set(webrender::PROFILER_DBG, opts.webrender_stats);

            webrender::Renderer::new(window.gl(), webrender::RendererOptions {
                device_pixel_ratio: device_pixel_ratio,
                resource_override_path: Some(resource_path),
                enable_aa: opts.enable_text_antialiasing,
                debug_flags: debug_flags,
                enable_batcher: opts.webrender_batch,
                debug: opts.webrender_debug,
                recorder: recorder,
                precache_shaders: opts.precache_shaders,
                enable_scrollbars: opts.output_file.is_none(),
                renderer_kind: renderer_kind,
                enable_subpixel_aa: opts.enable_subpixel_text_antialiasing,
                ..Default::default()
            }).expect("Unable to initialize webrender!")
        };

        let webrender_api = webrender_api_sender.create_api();
        let webrender_document = webrender_api.add_document(window.framebuffer_size());

        // Important that this call is done in a single-threaded fashion, we
        // can't defer it after `create_constellation` has started.
        script::init();

        // Create the constellation, which maintains the engine
        // pipelines, including the script and layout threads, as well
        // as the navigation context.
        let (constellation_chan, sw_senders) = create_constellation(opts.user_agent.clone(),
                                                                    opts.config_dir.clone(),
                                                                    compositor_proxy.clone_compositor_proxy(),
                                                                    time_profiler_chan.clone(),
                                                                    mem_profiler_chan.clone(),
                                                                    debugger_chan,
                                                                    devtools_chan,
                                                                    supports_clipboard,
                                                                    &mut webrender,
                                                                    webrender_document,
                                                                    webrender_api_sender);

        // Send the constellation's swmanager sender to service worker manager thread
        script::init_service_workers(sw_senders);

        if cfg!(feature = "webdriver") {
            if let Some(port) = opts.webdriver_port {
                webdriver(port, constellation_chan.clone());
            }
        }

        // The compositor coordinates with the client window to create the final
        // rendered page and display it somewhere.
        let compositor = IOCompositor::create(window, InitialCompositorState {
            sender: compositor_proxy,
            receiver: compositor_receiver,
            constellation_chan: constellation_chan.clone(),
            time_profiler_chan: time_profiler_chan,
            mem_profiler_chan: mem_profiler_chan,
            webrender,
            webrender_document,
            webrender_api,
        });

        Servo {
            compositor: compositor,
            constellation_chan: constellation_chan,
        }
    }

    pub fn handle_events(&mut self, events: Vec<WindowEvent>) -> bool {
        self.compositor.handle_events(events)
    }

    pub fn repaint_synchronously(&mut self) {
        self.compositor.repaint_synchronously()
    }

    pub fn pinch_zoom_level(&self) -> f32 {
        self.compositor.pinch_zoom_level()
    }

    pub fn setup_logging(&self) {
        let constellation_chan = self.constellation_chan.clone();
        log::set_logger(|max_log_level| {
            let env_logger = EnvLogger::new();
            let con_logger = FromCompositorLogger::new(constellation_chan);
            let filter = max(env_logger.filter(), con_logger.filter());
            let logger = BothLogger(env_logger, con_logger);
            max_log_level.set(filter);
            Box::new(logger)
        }).expect("Failed to set logger.")
    }

    pub fn deinit(self) {
        self.compositor.deinit();
    }
}

fn create_compositor_channel(event_loop_waker: Box<compositor_thread::EventLoopWaker>)
    -> (CompositorProxy, CompositorReceiver) {
    let (sender, receiver) = channel();
    (CompositorProxy {
         sender: sender,
         event_loop_waker: event_loop_waker,
        },
     CompositorReceiver {
         receiver: receiver
     })
}

fn create_constellation(user_agent: Cow<'static, str>,
                        config_dir: Option<PathBuf>,
                        compositor_proxy: CompositorProxy,
                        time_profiler_chan: time::ProfilerChan,
                        mem_profiler_chan: mem::ProfilerChan,
                        debugger_chan: Option<debugger::Sender>,
                        devtools_chan: Option<Sender<devtools_traits::DevtoolsControlMsg>>,
                        supports_clipboard: bool,
                        webrender: &mut webrender::Renderer,
                        webrender_document: webrender_api::DocumentId,
                        webrender_api_sender: webrender_api::RenderApiSender)
                        -> (Sender<ConstellationMsg>, SWManagerSenders) {
    let bluetooth_thread: IpcSender<BluetoothRequest> = BluetoothThreadFactory::new();

    let (public_resource_threads, private_resource_threads) =
        new_resource_threads(user_agent,
                             devtools_chan.clone(),
                             time_profiler_chan.clone(),
                             config_dir);
    let font_cache_thread = FontCacheThread::new(public_resource_threads.sender(),
                                                 Some(webrender_api_sender.create_api()));

    let resource_sender = public_resource_threads.sender();

    let (webvr_chan, webvr_constellation_sender, webvr_compositor) = if PREFS.is_webvr_enabled() {
        // WebVR initialization
        let (mut handler, sender) = WebVRCompositorHandler::new();
        let (webvr_thread, constellation_sender) = WebVRThread::spawn(sender);
        handler.set_webvr_thread_sender(webvr_thread.clone());
        (Some(webvr_thread), Some(constellation_sender), Some(handler))
    } else {
        (None, None, None)
    };

    // GLContext factory used to create WebGL Contexts
    let gl_factory = if opts::get().should_use_osmesa() {
        GLContextFactory::current_osmesa_handle().unwrap()
    } else {
        GLContextFactory::current_native_handle(&compositor_proxy).unwrap()
    };

    // Initialize WebGL Thread entry point.
    let (webgl_threads, image_handler) = WebGLThreads::new(gl_factory,
                                                           webrender_api_sender.clone(),
                                                           webvr_compositor.map(|c| c as Box<_>));
    // Set webrender external image handler for WebGL textures
    webrender.set_external_image_handler(image_handler);

    let initial_state = InitialConstellationState {
        compositor_proxy,
        debugger_chan,
        devtools_chan,
        bluetooth_thread,
        font_cache_thread,
        public_resource_threads,
        private_resource_threads,
        time_profiler_chan,
        mem_profiler_chan,
        supports_clipboard,
        webrender_document,
        webrender_api_sender,
        webgl_threads,
        webvr_chan,
    };
    let (constellation_chan, from_swmanager_sender) =
        Constellation::<script_layout_interface::message::Msg,
                        layout_thread::LayoutThread,
                        script::script_thread::ScriptThread>::start(initial_state);

    if let Some(webvr_constellation_sender) = webvr_constellation_sender {
        // Set constellation channel used by WebVR thread to broadcast events
        webvr_constellation_sender.send(constellation_chan.clone()).unwrap();
    }

    // channels to communicate with Service Worker Manager
    let sw_senders = SWManagerSenders {
        swmanager_sender: from_swmanager_sender,
        resource_sender: resource_sender
    };

    (constellation_chan, sw_senders)
}

// A logger that logs to two downstream loggers.
// This should probably be in the log crate.
struct BothLogger<Log1, Log2>(Log1, Log2);

impl<Log1, Log2> Log for BothLogger<Log1, Log2> where Log1: Log, Log2: Log {
    fn enabled(&self, metadata: &LogMetadata) -> bool {
        self.0.enabled(metadata) || self.1.enabled(metadata)
    }

    fn log(&self, record: &LogRecord) {
        self.0.log(record);
        self.1.log(record);
    }
}

pub fn set_logger(script_to_constellation_chan: ScriptToConstellationChan) {
    log::set_logger(|max_log_level| {
        let env_logger = EnvLogger::new();
        let con_logger = FromScriptLogger::new(script_to_constellation_chan);
        let filter = max(env_logger.filter(), con_logger.filter());
        let logger = BothLogger(env_logger, con_logger);
        max_log_level.set(filter);
        Box::new(logger)
    }).expect("Failed to set logger.")
}

/// Content process entry point.
pub fn run_content_process(token: String) {
    let (unprivileged_content_sender, unprivileged_content_receiver) =
        ipc::channel::<UnprivilegedPipelineContent>().unwrap();
    let connection_bootstrap: IpcSender<IpcSender<UnprivilegedPipelineContent>> =
        IpcSender::connect(token).unwrap();
    connection_bootstrap.send(unprivileged_content_sender).unwrap();

    let unprivileged_content = unprivileged_content_receiver.recv().unwrap();
    opts::set_defaults(unprivileged_content.opts());
    PREFS.extend(unprivileged_content.prefs());
    set_logger(unprivileged_content.script_to_constellation_chan().clone());

    // Enter the sandbox if necessary.
    if opts::get().sandbox {
       create_sandbox();
    }

    // send the required channels to the service worker manager
    let sw_senders = unprivileged_content.swmanager_senders();
    script::init();
    script::init_service_workers(sw_senders);

    unprivileged_content.start_all::<script_layout_interface::message::Msg,
                                     layout_thread::LayoutThread,
                                     script::script_thread::ScriptThread>(true);
}

// This is a workaround for https://github.com/rust-lang/rust/pull/30175 until
// https://github.com/lfairy/rust-errno/pull/5 lands, and should be removed once
// we update Servo with the rust-errno crate.
#[cfg(target_os = "android")]
#[no_mangle]
pub unsafe extern fn __errno_location() -> *mut i32 {
    extern { fn __errno() -> *mut i32; }
    __errno()
}

#[cfg(all(not(target_os = "windows"), not(target_os = "ios")))]
fn create_sandbox() {
    ChildSandbox::new(content_process_sandbox_profile()).activate()
        .expect("Failed to activate sandbox!");
}

#[cfg(any(target_os = "windows", target_os = "ios"))]
fn create_sandbox() {
    panic!("Sandboxing is not supported on Windows or iOS.");
}
