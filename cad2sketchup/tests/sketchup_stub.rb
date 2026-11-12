# Minimal stand-in for the SketchUp Ruby API, used only to smoke-test generated scripts in CI.
module Geom
  class Point3d; def initialize(*a); @a = a; end; end
  class PolygonMesh
    AUTO_SOFTEN = 4
    def initialize; @pts = []; @polys = []; end
    def add_point(p); @pts << p; @pts.size; end
    def add_polygon(*idx); raise ArgumentError, 'bad index' if idx.any? { |i| i.nil? || i > @pts.size }; @polys << idx; @polys.size; end
  end
end
module Sketchup
  STATS = Hash.new(0)
  class Color; def initialize(r, g, b); end; end
  class Face
    attr_accessor :material
    def initialize(pts); raise ArgumentError, 'face needs 3+ points' if pts.size < 3
      pts.each { |p| raise ArgumentError, 'bad point' unless p.is_a?(Array) && p.size == 3 && p.all? { |c| c.is_a?(Numeric) } }
      @pts = pts; STATS[:faces] += 1; end
    def normal; Struct.new(:z).new(-1.0); end
    def reverse!; self; end
    def pushpull(d); raise ArgumentError unless d.is_a?(Numeric); STATS[:pushpulls] += 1; end
    def valid?; true; end
    def erase!; end
    def area; 1.0; end
  end
  class Entities
    def initialize; @items = []; end
    def add_group; g = Group.new; @items << g; STATS[:groups] += 1; g; end
    def add_face(pts); f = Face.new(pts); @items << f; f; end
    def add_edges(pts); raise ArgumentError if pts.size < 2; STATS[:edges] += pts.size - 1; @items << :e; end
    def add_faces_from_mesh(m, flags, mat); STATS[:meshes] += 1; @items << :m; end
    def grep(k); @items.grep(k); end
    def length; @items.length; end
  end
  class Group
    attr_accessor :name, :layer
    def entities; @e ||= Entities.new; end
    def erase!; end
  end
  class Named; def initialize; @h = {}; end; def [](k); @h[k]; end
    def add(k); @h[k] = Struct.new(:name, :color).new(k); end; end
  class View; def zoom_extents; end; end
  class Model
    def active_entities; @e ||= Entities.new; end
    def layers; @l ||= Named.new; end
    def materials; @m ||= Named.new; end
    def start_operation(*); end
    def commit_operation; STATS[:committed] += 1; end
    def abort_operation; STATS[:aborted] += 1; end
    def active_view; View.new; end
  end
  def self.active_model; @model ||= Model.new; end
end
ARGV.each { |f| load f }
puts Sketchup::STATS.inspect
